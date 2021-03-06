# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

"""Train a Fast R-CNN network."""

import caffe
from fast_rcnn.config import cfg
import roi_data_layer.roidb as rdl_roidb
from utils.timer import Timer
import numpy as np
import os, time, sys
from multiprocessing import Process, Queue

from caffe.proto import caffe_pb2
import google.protobuf as pb2

class SolverWrapper(object):
    """A simple wrapper around Caffe's solver.
    This wrapper gives us control over he snapshotting process, which we
    use to unnormalize the learned bounding-box regression weights.
    """

    def __init__(self, solver_prototxt, roidb, output_dir, 
            nccl_uid, rank, bbox_means=None, bbox_stds=None, 
            pretrained_model=None):
        """Initialize the SolverWrapper."""
        self.output_dir = output_dir
        self.rank = rank
        if cfg.TRAIN.BBOX_REG:
            self.bbox_means, self.bbox_stds = bbox_means, bbox_stds
        if (cfg.TRAIN.HAS_RPN and cfg.TRAIN.BBOX_REG and
            cfg.TRAIN.BBOX_NORMALIZE_TARGETS):
            # RPN can only use precomputed normalization because there are no
            # fixed statistics to compute a priori
            assert cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED

        self.solver = caffe.SGDSolver(solver_prototxt)
        
        assert caffe.solver_count() * cfg.TRAIN.IMS_PER_BATCH * self.solver.param.iter_size == \
            cfg.TRAIN.REAL_BATCH_SIZE, "{} vs {}". \
            format(caffe.solver_count() * cfg.TRAIN.IMS_PER_BATCH * self.solver.param.iter_size, cfg.TRAIN.REAL_BATCH_SIZE)

        if pretrained_model is not None:
            print ('Loading pretrained model '
                   'weights from {:s}').format(pretrained_model)
            self.solver.net.copy_from(pretrained_model)

        nccl = caffe.NCCL(self.solver, nccl_uid)
        nccl.bcast()
        self.solver.add_callback(nccl)
        assert self.solver.param.layer_wise_reduce
        if self.solver.param.layer_wise_reduce:
            self.solver.net.after_backward(nccl)
        self.nccl = nccl # hold the reference to nccl

        self.solver_param = caffe_pb2.SolverParameter()
        with open(solver_prototxt, 'rt') as f:
            pb2.text_format.Merge(f.read(), self.solver_param)

        self.solver.net.layers[0].set_roidb(roidb)

    def snapshot(self):
        """Take a snapshot of the network after unnormalizing the learned
        bounding-box regression weights. This enables easy use at test-time.
        """
        assert self.rank==0
        net = self.solver.net

        scale_bbox_params = (cfg.TRAIN.BBOX_REG and
                             cfg.TRAIN.BBOX_NORMALIZE_TARGETS and
                             net.params.has_key('bbox_pred'))

        if scale_bbox_params:
            # save original values
            orig_0 = net.params['bbox_pred'][0].data.copy()
            orig_1 = net.params['bbox_pred'][1].data.copy()

            # scale and shift with bbox reg unnormalization; then save snapshot
            net.params['bbox_pred'][0].data[...] = \
                    (net.params['bbox_pred'][0].data *
                     self.bbox_stds[:, np.newaxis])
            net.params['bbox_pred'][1].data[...] = \
                    (net.params['bbox_pred'][1].data *
                     self.bbox_stds + self.bbox_means)

        infix = ('_' + cfg.TRAIN.SNAPSHOT_INFIX
                 if cfg.TRAIN.SNAPSHOT_INFIX != '' else '')
        filename = (self.solver_param.snapshot_prefix + infix +
                    '_iter_{:d}'.format(self.solver.iter) + '.caffemodel')
        filename = os.path.join(self.output_dir, filename)

        net.save(str(filename))
        print 'Wrote snapshot to: {:s}'.format(filename)

        if scale_bbox_params:
            # restore net to original state
            net.params['bbox_pred'][0].data[...] = orig_0
            net.params['bbox_pred'][1].data[...] = orig_1
        return filename

    def train_model(self, max_iters):
        """Network training loop."""
        last_snapshot_iter = -1
        timer = Timer()
        model_paths = []
        while self.solver.iter < max_iters:
            # Make one SGD update
            timer.tic()
            self.solver.step(1)
            timer.toc()
            
            if self.solver.iter % (10 * self.solver_param.display) == 0:
                sys.stderr.write('rank: {} iteration: {} speed: {:.3f}s / iter\n'.format(self.rank, self.solver.iter, timer.average_time))

            if self.rank == 0 and self.solver.iter % cfg.TRAIN.SNAPSHOT_ITERS == 0:
                last_snapshot_iter = self.solver.iter
                model_paths.append(self.snapshot())

        if self.rank == 0 and last_snapshot_iter != self.solver.iter:
            model_paths.append(self.snapshot())
        return model_paths

def get_training_roidb(imdb):
    """Returns a roidb (Region of Interest database) for use in training."""
    if cfg.TRAIN.USE_FLIPPED:
        print 'Appending horizontally-flipped training examples...'
        imdb.append_flipped_images()
        print 'done'

    print 'Preparing training data...'
    rdl_roidb.prepare_roidb(imdb)
    print 'done'

    return imdb.roidb

def filter_roidb(roidb):
    """Remove roidb entries that have no usable RoIs."""

    def is_valid(entry):
        # Valid images have:
        #   (1) At least one foreground RoI OR
        #   (2) At least one background RoI
        overlaps = entry['max_overlaps']
        # find boxes with sufficient overlap
        fg_inds = np.where(overlaps >= cfg.TRAIN.FG_THRESH)[0]
        # Select background RoIs as those within [BG_THRESH_LO, BG_THRESH_HI)
        bg_inds = np.where((overlaps < cfg.TRAIN.BG_THRESH_HI) &
                           (overlaps >= cfg.TRAIN.BG_THRESH_LO))[0]
        # image is only valid if such boxes exist
        valid = len(fg_inds) > 0 or len(bg_inds) > 0
        w = entry["width"]
        h = entry["height"]
        ratio = min(h, w) / float(max(h, w))
        return ratio > cfg.TRAIN.MIN_IM_RATIO and valid

    num = len(roidb)
    filtered_roidb = [entry for entry in roidb if is_valid(entry)]
    num_after = len(filtered_roidb)
    print 'Filtered {} roidb entries: {} -> {}'.format(num - num_after,
                                                       num, num_after)
    return filtered_roidb

def train_net_multi_gpus(solver_prototxt, roidb, output_dir,
        gpus, pretrained_model=None, max_iters=40000):
    roidb = filter_roidb(roidb)
    nccl_uid = caffe.NCCL.new_uid()
    print 'Solving...'
    if cfg.TRAIN.BBOX_REG:
        print 'Computing bounding-box regression targets...'
        bbox_means, bbox_stds = \
                rdl_roidb.add_bbox_regression_targets(roidb)
        print 'done'
    else:
        bbox_means, bbox_stds = None, None
    mp_queue = Queue()
    procs=[]
    for rank in range(len(gpus)):
        p = Process(target=train_net,
                    args=(solver_prototxt, roidb, output_dir, 
                        nccl_uid, gpus, rank, mp_queue, 
                        bbox_means, bbox_stds, 
                        pretrained_model, max_iters))
        p.daemon = True
        p.start()
        procs.append(p)
    for p in procs:
        p.join()
    print 'done solving'
    return mp_queue.get() # return the result of root_solver (rank==0)

def train_net(solver_prototxt, roidb, output_dir, nccl_uid, gpus, rank,
        queue, bbox_means, bbox_stds, pretrained_model=None, max_iters=40000):
    """Train a Fast R-CNN network."""
    caffe.set_mode_gpu()
    caffe.set_device(gpus[rank])
    caffe.set_solver_count(len(gpus))
    caffe.set_solver_rank(rank)
    caffe.set_multiprocess(True)
    caffe.set_random_seed(cfg.RNG_SEED)
    sw = SolverWrapper(solver_prototxt, roidb, output_dir, nccl_uid, 
        rank, bbox_means, bbox_stds, pretrained_model=pretrained_model)
    model_paths = sw.train_model(max_iters)
    if rank==0:
        queue.put(model_paths)
