'search stuff'

import os
import itertools as it
import numpy as np
from xbin import XformBinner
from homog import hinv, hrot
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from .worms import Segment, Segments, Worms
from .criteria import CriteriaList, Cyclic, WormCriteria
from . import util


class SimpleAccumulator:

    def __init__(self, max_results=1000000, max_tmp_size=1024):
        self.max_tmp_size = max_tmp_size
        self.max_results = max_results
        self.temporary = []

    def checkpoint(self):
        if len(self.temporary) is 0: return
        if hasattr(self, 'scores'):
            sc, li, lp = [self.scores], [self.lowidx], [self.lowpos]
        else:
            sc, li, lp = [], [], []
        scores = np.concatenate([x[0] for x in self.temporary] + sc)
        lowidx = np.concatenate([x[1] for x in self.temporary] + li)
        lowpos = np.concatenate([x[2] for x in self.temporary] + lp)
        order = np.argsort(scores)
        self.scores = scores[order[:self.max_results]]
        self.lowidx = lowidx[order[:self.max_results]]
        self.lowpos = lowpos[order[:self.max_results]]
        self.temporary = []

    def accumulate(self, gen):
        for future in gen:
            result = future.result()
            if result is not None:
                self.temporary.append(result)
                if len(self.temporary) >= self.max_tmp_size:
                    self.checkpoint()
            yield None

    def final_result(self):
        self.checkpoint()
        try:
            return self.scores, self.lowidx, self.lowpos
        except AttributeError:
            return None


class IndexAccumulator:

    def __init__(self, sizes, thresh=1, from_seg=0, to_seg=-1,
                 max_tmp_size=1024, cart_resl=2.0, ori_resl=15.0):
        self.sizes = sizes
        self.thresh = thresh
        self.max_tmp_size = max_tmp_size
        self.from_seg = from_seg
        self.to_seg = to_seg
        self.tmp = []
        self.binner = XformBinner(cart_resl, ori_resl)
        self.index = dict()

    def checkpoint(self):
        if len(self.tmp) is 0: return
        sc = np.concatenate([x[0] for x in self.tmp])
        indices = np.concatenate([x[1] for x in self.tmp])[sc <= self.thresh]
        assert np.all(indices < self.sizes)
        positions = np.concatenate([x[2] for x in self.tmp])[sc <= self.thresh]
        from_pos = positions[:, self.from_seg]
        to_pos = positions[:, self.to_seg]
        xtgt = hinv(from_pos) @ to_pos
        binidx = self.binner.get_bin_index(xtgt)
        self.index = {**{k: v for k, v in zip(binidx, indices)}, **self.index}
        # print('IndexAcculator checkpoint, index size:', len(self.index))
        self.tmp = []

    def accumulate(self, gen):
        for future in gen:
            result = future.result()
            if result is not None:
                self.tmp.append(result)
                if len(self.tmp) >= self.max_tmp_size:
                    self.checkpoint()
            yield None

    def final_result(self):
        self.checkpoint()
        return self.index


class IndexedCriteria(WormCriteria):

    def __init__(self, index, binner, nfold, from_seg=-1):
        self.index = index
        self.binner = binner
        self.from_seg = from_seg
        self.cyclic_xform = hrot([0, 0, 1], 360 / nfold)

    def score(self, segpos, **kw):
        from_pos = segpos[self.from_seg]
        to_pos = self.cyclic_xform @ from_pos
        xtgt = hinv(from_pos) @ to_pos
        binidx = self.binner.get_bin_index(xtgt)
        is_in_index = np.vectorize(lambda i: 0 if i in self.index else 9e9)
        return is_in_index(binidx)

    def alignment(self, segpos, **kw):
        return np.eye(4)


class IndexedAccumulator:

    def __init__(self, tail, splitseg, head, index, binner,
                 nfold, from_seg=-1, max_tmp_size=1024):
        self.splitseg
        self.tail = tail
        self.head = head
        self.index = index
        self.binner = binner
        self.from_seg = from_seg
        self.cyclic_xform = hrot([0, 0, 1], 360 / nfold)
        self.max_tmp_size = max_tmp_size
        self.temporary = []

    def checkpoint(self):
        if len(self.temporary) is 0: return
        if hasattr(self, 'scores'):
            sc, li, lp = [self.scores], [self.lowidx], [self.lowpos]
        else:
            sc, li, lp = [], [], []
        scores = np.concatenate([x[0] for x in self.temporary])
        print('IndexedAccumulator checkpoint scores.shape', scores.shape)
        if scores.shape[0] is 0: return
        assert np.all(scores == 0)
        lowidx = np.concatenate([x[1] for x in self.temporary])
        lowpos = np.concatenate([x[2] for x in self.temporary])
        from_pos = lowpos[:, self.from_seg]
        to_pos = self.cyclic_xform @ from_pos
        xtgt = hinv(from_pos) @ to_pos
        binidx = self.binner.get_bin_index(xtgt)
        head_idx = np.stack([self.index[i] for i in binidx])
        join_idx = self.splitseg.merge_idx(self.tail[-1], lowidx[:, -1],
                                           self.head[0], head_idx[:, 0])
        print(head_idx.shape)
        print(join_idx.shape)
        lowidx = np.concatenate(
            [lowidx[:, :-1], join_idx, head_idx[:, 1:]], axis=1)
        if hasattr(self, 'lowidx'):
            self.lowidx = np.concatenate(self.lowidx, lowidx)
        else:
            self.lowidx = lowidx
        self.temporary = []

    def accumulate(self, gen):
        for future in gen:
            result = future.result()
            if result is not None:
                self.temporary.append(result)
                if len(self.temporary) >= self.max_tmp_size:
                    self.checkpoint()
            yield None

    def final_result(self):
        self.checkpoint()
        try:
            return self.lowidx
        except AttributeError:
            return None


def _get_chunk_end_seg(sizes, max_workers, memsize):
    end = len(sizes) - 1
    while end > 1 and (util.bigprod(sizes[end:]) < max_workers or
                       memsize <= 64 * util.bigprod(sizes[:end])): end -= 1
    return end


def grow(
    segments,
    criteria,
    *,
    thresh=2,
    expert=0,
    memsize=1e6,
    executor=None,
    executor_args=None,
    max_workers=None,
    verbosity=0,
    chunklim=None,
    max_samples=int(1e12),
    max_results=int(1e4),
    cart_resl=2.0,
    ori_resl=15.0
):
    if True:  # setup
        os.environ['OMP_NUM_THREADS'] = '1'
        os.environ['MKL_NUM_THREADS'] = '1'
        os.environ['NUMEXPR_NUM_THREADS'] = '1'
        if isinstance(segments, list):
            segments = Segments(segments)
        # if isinstance(executor, (ProcessPoolExecutor, ThreadPoolExecutor)):
            # raise ValueError('please use dask.distributed executor')
        if verbosity > 0:
            print('grow, from', criteria.from_seg, 'to', criteria.to_seg)
            for i, seg in enumerate(segments):
                print(
                    ' segment',
                    i,
                    'enter:',
                    seg.entrypol,
                    'exit:',
                    seg.exitpol)
                for sp in seg.spliceables: print('   ', sp)
        if verbosity > 1:
            global __print_best
            __print_best = True
        if not isinstance(criteria, CriteriaList):
            criteria = CriteriaList(criteria)
        if max_workers is not None and max_workers <= 0:
            max_workers = util.cpu_count()
        if executor_args is None and max_workers is None:
            executor_args = dict()
        elif executor_args is None:
            executor_args = dict(max_workers=max_workers)
        elif executor_args is not None and max_workers is not None:
            raise ValueError('executor_args incompatible with max_workers')

        # checks and setup
        matchlast = _check_topology(segments, criteria, expert)
        if executor is None:
            executor = ThreadPoolExecutor  # todo: some kind of null executor?
            max_workers = 1
        if max_workers is None: max_workers = util.cpu_count()
        sizes = [len(s) for s in segments]
        end = _get_chunk_end_seg(sizes, max_workers, memsize)
        ntot, chunksize, nchunks = (util.bigprod(x)
                                    for x in (sizes, sizes[:end], sizes[end:]))
        if max_samples is not None:
            max_samples = np.clip(chunksize * max_workers, max_samples, ntot)
        every_other = max(1, int(ntot / max_samples)) if max_samples else 1
        nworker = max_workers or util.cpu_count()
        njob = int(np.sqrt(nchunks / every_other) / 128) * nworker
        njob = np.clip(nworker, njob, nchunks)

        actual_ntot = int(ntot / every_other)
        actual_nchunk = int(nchunks / every_other)
        actual_perjob = int(ntot / every_other / njob)
        actual_chunkperjob = int(nchunks / every_other / njob)

    if verbosity >= 0:
        print('tot: {:,} chunksize: {:,} nchunks: {:,} nworker: {} njob: {}'.format(
            ntot, chunksize, nchunks, nworker, njob))
        print('worm/job: {:,} chunk/job: {} sizes={} every_other={}'.format(
            int(ntot / njob), int(nchunks / njob), sizes, every_other))
        print('max_samples: {:,} max_results: {:,}'.format(
            max_samples, max_results))
        print('actual tot:        {:,}'.format(int(actual_ntot)))
        print('actual nchunks:    {:,}'.format(int(actual_nchunk)))
        print('actual worms/job:  {:,}'.format(int(actual_perjob)))
        print('actual chunks/job: {:,}'.format(int(actual_chunkperjob)))

    if njob > 1e9 or nchunks >= 2**63 or every_other >= 2**63:
        print('too big?!?')
        print('    njob', njob)
        print('    nchunks', nchunks, nchunks / 2**63)
        print('    every_other', every_other, every_other / 2**63)
        raise ValueError('system too big')

    _grow_args = dict(executor=executor, executor_args=executor_args,
                      njob=njob, end=end, thresh=thresh,
                      matchlast=matchlast, every_other=every_other,
                      max_results=max_results, nworker=nworker,
                      verbosity=verbosity)

    if criteria.origin_seg is None:
        accum = SimpleAccumulator(max_results=max_results, max_tmp_size=1e5)
        _grow(segments, criteria, accum, **_grow_args)
        result = accum.final_result()
        if result is None: return None
        scores, lowidx, lowpos = result
        lowposlist = [lowpos[:, i] for i in range(len(segments))]
        score_check = criteria.score(segpos=lowposlist, verbosity=verbosity)
        assert np.allclose(score_check, scores)
        detail = dict(ntot=ntot, chunksize=chunksize, nchunks=nchunks,
                      nworker=nworker, njob=njob, sizes=sizes, end=end)
        return Worms(segments, scores, lowidx, lowpos, criteria, detail)
    else:
        assert len(criteria) is 1
        splitseg = segments[criteria.from_seg]
        tail, head = segments.split_at(criteria.from_seg)
        headsizes = [len(s) for s in head]
        accum1 = IndexAccumulator(headsizes, from_seg=0, to_seg=-1,
                                  cart_resl=cart_resl, ori_resl=ori_resl)
        headcriteria = Cyclic(criteria[0].nfold, from_seg=0, to_seg=-1,
                              tol=criteria[0].tol, lever=criteria[0].lever)
        args = _grow_args
        args['end'] = _get_chunk_end_seg(headsizes, max_workers, memsize)
        args['matchlast'] = 0  # kinda hacky
        _grow(head, headcriteria, accum1, **_grow_args)
        index = accum1.final_result()
        print('len(index)', len(index))
        tailcriteria = IndexedCriteria(accum1.index, accum1.binner,
                                       criteria[0].nfold, from_seg=-1)
        accum2 = IndexedAccumulator(tail, splitseg, head, accum1.index,
                                    accum1.binner, criteria[0].nfold,
                                    from_seg=-1)
        args['matchlast'] = None
        _grow(tail, tailcriteria, accum2, **_grow_args)
        result = accum2.final_result()
        if result is not None:
            print(result.shape)
        else:
            print('no results')


def _chain_xforms(segments):
    x2exit = [s.x2exit for s in segments]
    x2orgn = [s.x2orgn for s in segments]
    fullaxes = (np.newaxis,) * (len(x2exit) - 1)
    xconn = [x2exit[0][fullaxes], ]
    xbody = [x2orgn[0][fullaxes], ]
    for iseg in range(1, len(x2exit)):
        fullaxes = (slice(None),) + (np.newaxis,) * iseg
        xconn.append(xconn[iseg - 1] @ x2exit[iseg][fullaxes])
        xbody.append(xconn[iseg - 1] @ x2orgn[iseg][fullaxes])
    perm = list(range(len(xbody) - 1, -1, -1)) + [len(xbody), len(xbody) + 1]
    xbody = [np.transpose(x, perm) for x in xbody]
    xconn = [np.transpose(x, perm) for x in xconn]
    return xbody, xconn


__print_best = False
__best_score = 9e9


def _grow_chunk(samp, segpos, conpos, context):
    os.environ['OMP_NUM_THREADS'] = '1'
    os.environ['MKL_NUM_THREADS'] = '1'
    os.environ['NUMEXPR_NUM_THREADS'] = '1'
    _, _, segs, end, criteria, thresh, matchlast, _, max_results = context
    # print('_grow_chunk', samp, end, thresh, matchlast, max_results)
    ML = matchlast
    # body must match, and splice sites must be distinct
    if ML is not None:
        # print('  ML')
        ndimchunk = segpos[0].ndim - 2
        bidB = segs[-1].bodyid[samp[-1]]
        site3 = segs[-1].entrysiteid[samp[-1]]
        if ML < ndimchunk:
            bidA = segs[ML].bodyid
            site1 = segs[ML].entrysiteid
            site2 = segs[ML].exitsiteid
            allowed = (bidA == bidB) * (site1 != site3) * (site2 != site3)
            idx = (slice(None),) * ML + (allowed,)
            segpos = segpos[: ML] + [x[idx] for x in segpos[ML:]]
            conpos = conpos[: ML] + [x[idx] for x in conpos[ML:]]
            idxmap = np.where(allowed)[0]
        else:
            bidA = segs[ML].bodyid[samp[ML - ndimchunk]]
            site1 = segs[ML].entrysiteid[samp[ML - ndimchunk]]
            site2 = segs[ML].exitsiteid[samp[ML - ndimchunk]]
            if bidA != bidB or site3 == site2 or site3 == site1:
                return
    segpos, conpos = segpos[:end], conpos[:end]
    # print('  do geom')
    for iseg, seg in enumerate(segs[end:]):
        segpos.append(conpos[-1] @ seg.x2orgn[samp[iseg]])
        if seg is not segs[-1]:
            conpos.append(conpos[-1] @ seg.x2exit[samp[iseg]])
    # print('  scoring')
    score = criteria.score(segpos=segpos)
    # print('  scores shape', score.shape)
    if __print_best:
        global __best_score
        min_score = np.min(score)
        if min_score < __best_score:
            __best_score = min_score
            if __best_score < thresh * 5:
                print('best for pid %6i %7.3f' % (os.getpid(), __best_score))
    # print('  trimming max_results')
    ilow0 = np.where(score < thresh)
    if len(ilow0) > max_results:
        order = np.argsort(score[ilow0])
        ilow0 = ilow0[order[:max_results]]
    sampidx = tuple(np.repeat(i, len(ilow0[0])) for i in samp)
    lowpostmp = []
    # print('  make lowpos')
    for iseg in range(len(segpos)):
        ilow = ilow0[: iseg + 1] + (0,) * (segpos[0].ndim - 2 - (iseg + 1))
        lowpostmp.append(segpos[iseg][ilow])
    ilow1 = (ilow0 if (ML is None or ML >= ndimchunk) else
             ilow0[:ML] + (idxmap[ilow0[ML]],) + ilow0[ML + 1:])
    return score[ilow0], np.array(ilow1 + sampidx).T, np.stack(lowpostmp, 1)


def _grow_chunks(ijob, context):
    os.environ['OMP_NUM_THREADS'] = '1'
    os.environ['MKL_NUM_THREADS'] = '1'
    os.environ['NUMEXPR_NUM_THREADS'] = '1'
    sampsizes, njob, segments, end, _, _, _, every_other, max_results = context
    samples = list(util.MultiRange(sampsizes)[ijob::njob * every_other])
    segpos, connpos = _chain_xforms(segments[:end])  # common data
    args = [samples, it.repeat(segpos),
            it.repeat(connpos), it.repeat(context)]
    chunks = list(map(_grow_chunk, *args))
    chunks = [c for c in chunks if c is not None]
    if not chunks: return None
    scores = np.concatenate([x[0] for x in chunks])
    lowidx = np.concatenate([x[1] for x in chunks])
    lowpos = np.concatenate([x[2] for x in chunks])
    order = np.argsort(scores)
    return [scores[order[:max_results]],
            lowidx[order[:max_results]],
            lowpos[order[:max_results]]]


def _check_topology(segments, criteria, expert=False):
    if segments[0].entrypol is not None:
        raise ValueError('beginning of worm cant have entry')
    if segments[-1].exitpol is not None:
        raise ValueError('end of worm cant have exit')
    for a, b in zip(segments[:-1], segments[1:]):
        if not (a.exitpol and b.entrypol and a.exitpol != b.entrypol):
            raise ValueError('incompatible exit->entry polarity: '
                             + str(a.exitpol) + '->'
                             + str(b.entrypol) + ' on segment pair: '
                             + str((segments.index(a), segments.index(b))))
    matchlast = criteria.last_body_same_as
    if matchlast is not None and not expert and (
            not segments[matchlast].same_bodies_as(segments[-1])):
        raise ValueError("segments[matchlast] not same as segments[-1], "
                         + "if you're sure, pass expert=True")
    if criteria.is_cyclic and not criteria.to_seg in (-1, len(segments) - 1):
        raise ValueError('Cyclic and to_seg is not last segment,'
                         'if you\'re sure, pass expert=True')
    if criteria.is_cyclic:
        beg, end = segments[criteria.from_seg], segments[criteria.to_seg]
        sites_required = {'N': 0, 'C': 0, None: 0}
        sites_required[beg.entrypol] += 1
        sites_required[beg.exitpol] += 1
        sites_required[end.entrypol] += 1
        # print('pols', beg.entrypol, beg.exitpol, end.entrypol)
        for pol in 'NC':
            # print(pol, beg.max_sites[pol], sites_required[pol])
            if beg.max_sites[pol] < sites_required[pol]:
                msg = 'Not enough %s sites in any of segment %i Spliceables, %i required, at most %i available' % (
                    pol, criteria.from_seg, sites_required[pol],
                    beg.max_sites[pol])
                raise ValueError(msg)
            if beg.min_sites[pol] < sites_required[pol]:
                msg = 'Not enough %s sites in all of segment %i Spliceables, %i required, some have only %i available (pass expert=True if you really want to run anyway)' % (
                    pol, criteria.from_seg, sites_required[pol],
                    beg.max_sites[pol])
                if not expert: raise ValueError(msg)
                print("WARNING:", msg)
    return matchlast


def _grow(segments, criteria, accumulator, **kw):
    # terrible hack... xfering the poses too expensive
    tmp = {spl: (spl.body, spl.chains)
           for seg in segments for spl in seg.spliceables}
    for seg in segments:
        for spl in seg.spliceables:
            spl.body, spl.chains = None, None  # poses not pickleable...

    sizes = [len(s) for s in segments]
    ntot = util.bigprod(sizes)
    with kw['executor'](**kw['executor_args']) as pool:
        context = (sizes[kw['end']:], kw['njob'], segments, kw['end'],
                   criteria, kw['thresh'], kw['matchlast'], kw['every_other'],
                   kw['max_results'])
        args = [range(kw['njob'])] + [it.repeat(context)]
        util.tqdm_parallel_map(
            pool=pool,
            function=_grow_chunks,
            accumulator=accumulator,
            map_func_args=args,
            batch_size=kw['nworker'] * 8,
            unit='K worms',
            ascii=0,
            desc='growing worms',
            unit_scale=int(ntot / kw['njob'] / 1000 / kw['every_other']),
            disable=kw['verbosity'] < 0
        )

    # put the poses back...
    for seg in segments:
        for spl in seg.spliceables:
            spl.body, spl.chains = tmp[spl]
