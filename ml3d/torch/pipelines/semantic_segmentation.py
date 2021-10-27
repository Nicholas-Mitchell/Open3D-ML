import logging
from os.path import exists, join
from pathlib import Path
from datetime import datetime

import numpy as np
from tqdm import tqdm
import torch
from torch.utils.tensorboard import SummaryWriter
from torch.utils.data import DataLoader

# pylint: disable-next=unused-import
from open3d.visualization.tensorboard_plugin import summary
from .base_pipeline import BasePipeline
from ..dataloaders import get_sampler, TorchDataloader, DefaultBatcher, ConcatBatcher
from ..utils import latest_torch_ckpt
from ..modules.losses import SemSegLoss
from ..modules.metrics import SemSegMetric
from ...utils import make_dir, LogRecord, PIPELINE, get_runid, code2md
from ...datasets import InferenceDummySplit

logging.setLogRecordFactory(LogRecord)
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(asctime)s - %(module)s - %(message)s',
)
log = logging.getLogger(__name__)

import ipdb


class SemanticSegmentation(BasePipeline):
    """This class allows you to perform semantic segmentation for both training
    and inference using the Torch. This pipeline has multiple stages: Pre-
    processing, loading dataset, testing, and inference or training.

    **Example:**
        This example loads the Semantic Segmentation and performs a training
        using the SemanticKITTI dataset.

            import torch
            import torch.nn as nn

            from .base_pipeline import BasePipeline
            from torch.utils.tensorboard import SummaryWriter
            from ..dataloaders import get_sampler, TorchDataloader, DefaultBatcher, ConcatBatcher

            Mydataset = TorchDataloader(dataset=dataset.get_split('training')),
            MyModel = SemanticSegmentation(self,model,dataset=Mydataset, name='SemanticSegmentation',
            name='MySemanticSegmentation',
            batch_size=4,
            val_batch_size=4,
            test_batch_size=3,
            max_epoch=100,
            learning_rate=1e-2,
            lr_decays=0.95,
            save_ckpt_freq=20,
            adam_lr=1e-2,
            scheduler_gamma=0.95,
            momentum=0.98,
            main_log_dir='./logs/',
            device='gpu',
            split='train',
            train_sum_dir='train_log')

    **Args:**
            dataset: The 3D ML dataset class. You can use the base dataset, sample datasets , or a custom dataset.
            model: The model to be used for building the pipeline.
            name: The name of the current training.
            batch_size: The batch size to be used for training.
            val_batch_size: The batch size to be used for validation.
            test_batch_size: The batch size to be used for testing.
            max_epoch: The maximum size of the epoch to be used for training.
            leanring_rate: The hyperparameter that controls the weights during training. Also, known as step size.
            lr_decays: The learning rate decay for the training.
            save_ckpt_freq: The frequency in which the checkpoint should be saved.
            adam_lr: The leanring rate to be applied for Adam optimization.
            scheduler_gamma: The decaying factor associated with the scheduler.
            momentum: The momentum that accelerates the training rate schedule.
            main_log_dir: The directory where logs are stored.
            device: The device to be used for training.
            split: The dataset split to be used. In this example, we have used "train".
            train_sum_dir: The directory where the trainig summary is stored.

    **Returns:**
            class: The corresponding class.
    """

    def __init__(
            self,
            model,
            dataset=None,
            name='SemanticSegmentation',
            batch_size=4,
            val_batch_size=4,
            test_batch_size=3,
            max_epoch=100,  # maximum epoch during training
            learning_rate=1e-2,  # initial learning rate
            lr_decays=0.95,
            save_ckpt_freq=20,
            adam_lr=1e-2,
            scheduler_gamma=0.95,
            momentum=0.98,
            main_log_dir='./logs/',
            device='gpu',
            split='train',
            train_sum_dir='train_log',
            **kwargs):

        super().__init__(model=model,
                         dataset=dataset,
                         name=name,
                         batch_size=batch_size,
                         val_batch_size=val_batch_size,
                         test_batch_size=test_batch_size,
                         max_epoch=max_epoch,
                         learning_rate=learning_rate,
                         lr_decays=lr_decays,
                         save_ckpt_freq=save_ckpt_freq,
                         adam_lr=adam_lr,
                         scheduler_gamma=scheduler_gamma,
                         momentum=momentum,
                         main_log_dir=main_log_dir,
                         device=device,
                         split=split,
                         train_sum_dir=train_sum_dir,
                         **kwargs)

    def run_inference(self, data):
        """Run inference on given data.

        Args:
            data: A raw data.
        Returns:
            Returns the inference results.
        """
        cfg = self.cfg
        model = self.model
        device = self.device

        model.to(device)
        model.device = device
        model.eval()

        batcher = self.get_batcher(device)
        infer_dataset = InferenceDummySplit(data)
        self.dataset_split = infer_dataset
        infer_sampler = infer_dataset.sampler
        infer_split = TorchDataloader(dataset=infer_dataset,
                                      preprocess=model.preprocess,
                                      transform=model.transform,
                                      sampler=infer_sampler,
                                      use_cache=False)
        infer_loader = DataLoader(infer_split,
                                  batch_size=cfg.batch_size,
                                  sampler=get_sampler(infer_sampler),
                                  collate_fn=batcher.collate_fn)

        model.trans_point_sampler = infer_sampler.get_point_sampler()
        self.curr_cloud_id = -1
        self.test_probs = []
        self.test_labels = []
        self.ori_test_probs = []
        self.ori_test_labels = []

        with torch.no_grad():
            for unused_step, inputs in enumerate(infer_loader):
                results = model(inputs['data'])
                self.update_tests(infer_sampler, inputs, results)

        inference_result = {
            'predict_labels': self.ori_test_labels.pop(),
            'predict_scores': self.ori_test_probs.pop()
        }

        metric = SemSegMetric()
        metric.update(torch.tensor(inference_result['predict_scores']),
                      torch.tensor(data['label']))
        log.info(f"Accuracy : {metric.acc()}")
        log.info(f"IoU : {metric.iou()}")

        return inference_result

    def run_test(self):
        """Run the test using the data passed.
        """
        model = self.model
        dataset = self.dataset
        device = self.device
        cfg = self.cfg
        model.device = device
        model.to(device)
        model.eval()

        timestamp = datetime.now().strftime('%Y-%m-%d_%H:%M:%S')

        log.info("DEVICE : {}".format(device))
        log_file_path = join(cfg.logs_dir, 'log_test_' + timestamp + '.txt')
        log.info("Logging in file : {}".format(log_file_path))
        log.addHandler(logging.FileHandler(log_file_path))

        batcher = self.get_batcher(device)

        test_dataset = dataset.get_split('test')
        test_sampler = test_dataset.sampler
        test_split = TorchDataloader(dataset=test_dataset,
                                     preprocess=model.preprocess,
                                     transform=model.transform,
                                     sampler=test_sampler,
                                     use_cache=dataset.cfg.use_cache)
        test_loader = DataLoader(test_split,
                                 batch_size=cfg.test_batch_size,
                                 sampler=get_sampler(test_sampler),
                                 collate_fn=batcher.collate_fn)

        self.dataset_split = test_dataset

        self.load_ckpt(model.cfg.ckpt_path)

        model.trans_point_sampler = test_sampler.get_point_sampler()
        self.curr_cloud_id = -1
        self.test_probs = []
        self.test_labels = []
        self.ori_test_probs = []
        self.ori_test_labels = []
        self.summary = {'test': {}}

        record_summary = 'test' in cfg.get('summary').get('record_for', [])
        log.info("Started testing")

        with torch.no_grad():
            for unused_step, inputs in enumerate(test_loader):
                if hasattr(inputs['data'], 'to'):
                    inputs['data'].to(device)
                results = model(inputs['data'])
                self.update_tests(test_sampler, inputs, results)

                if self.complete_infer:
                    inference_result = {
                        'predict_labels': self.ori_test_labels.pop(),
                        'predict_scores': self.ori_test_probs.pop()
                    }
                    attr = self.dataset_split.get_attr(test_sampler.cloud_id)
                    dataset.save_test_result(inference_result, attr)
                    # Save only for the first batch
                    if record_summary and 'test' not in self.summary:
                        self.summary['test'] = self.get_3d_summary(
                            results, inputs, 0)

        log.info("Finshed testing")

    def update_tests(self, sampler, inputs, results):
        """Update tests using sampler, inputs, and results.
        """
        split = sampler.split
        end_threshold = 0.5
        if self.curr_cloud_id != sampler.cloud_id:
            self.curr_cloud_id = sampler.cloud_id
            num_points = sampler.possibilities[sampler.cloud_id].shape[0]
            self.pbar = tqdm(total=num_points,
                             desc="{} {}/{}".format(split, self.curr_cloud_id,
                                                    len(sampler.dataset)))
            self.pbar_update = 0
            self.test_probs.append(
                np.zeros(shape=[num_points, self.model.cfg.num_classes],
                         dtype=np.float16))
            self.test_labels.append(np.zeros(shape=[num_points],
                                             dtype=np.int16))
            self.complete_infer = False

        this_possiblility = sampler.possibilities[sampler.cloud_id]
        self.pbar.update(
            this_possiblility[this_possiblility > end_threshold].shape[0] -
            self.pbar_update)
        self.pbar_update = this_possiblility[
            this_possiblility > end_threshold].shape[0]
        self.test_probs[self.curr_cloud_id], self.test_labels[
            self.curr_cloud_id] = self.model.update_probs(
                inputs, results, self.test_probs[self.curr_cloud_id],
                self.test_labels[self.curr_cloud_id])

        if (split in ['test'] and
                this_possiblility[this_possiblility > end_threshold].shape[0]
                == this_possiblility.shape[0]):

            proj_inds = self.model.preprocess(
                self.dataset_split.get_data(self.curr_cloud_id), {
                    'split': split
                }).get('proj_inds', None)
            if proj_inds is None:
                proj_inds = np.arange(
                    self.test_probs[self.curr_cloud_id].shape[0])
            self.ori_test_probs.append(
                self.test_probs[self.curr_cloud_id][proj_inds])
            self.ori_test_labels.append(
                self.test_labels[self.curr_cloud_id][proj_inds])
            self.complete_infer = True

    def run_train(self):
        """Run the training on the self model.
        """
        model = self.model
        device = self.device
        model.device = device
        dataset = self.dataset

        cfg = self.cfg
        model.to(device)

        log.info("DEVICE : {}".format(device))
        timestamp = datetime.now().strftime('%Y-%m-%d_%H:%M:%S')

        log_file_path = join(cfg.logs_dir, 'log_train_' + timestamp + '.txt')
        log.info("Logging in file : {}".format(log_file_path))
        log.addHandler(logging.FileHandler(log_file_path))

        Loss = SemSegLoss(self, model, dataset, device)
        self.metric_train = SemSegMetric()
        self.metric_val = SemSegMetric()

        self.batcher = self.get_batcher(device)

        train_dataset = dataset.get_split('train')
        train_sampler = train_dataset.sampler
        train_split = TorchDataloader(dataset=train_dataset,
                                      preprocess=model.preprocess,
                                      transform=model.transform,
                                      sampler=train_sampler,
                                      use_cache=dataset.cfg.use_cache,
                                      steps_per_epoch=dataset.cfg.get(
                                          'steps_per_epoch_train', None))

        train_loader = DataLoader(
            train_split,
            batch_size=cfg.batch_size,
            sampler=get_sampler(train_sampler),
            num_workers=cfg.get('num_workers', 2),
            pin_memory=cfg.get('pin_memory', True),
            collate_fn=self.batcher.collate_fn,
            worker_init_fn=lambda x: np.random.seed(x + np.uint32(
                torch.utils.data.get_worker_info().seed))
        )  # numpy expects np.uint32, whereas torch returns np.uint64.

        valid_dataset = dataset.get_split('validation')
        valid_sampler = valid_dataset.sampler
        valid_split = TorchDataloader(dataset=valid_dataset,
                                      preprocess=model.preprocess,
                                      transform=model.transform,
                                      sampler=valid_sampler,
                                      use_cache=dataset.cfg.use_cache,
                                      steps_per_epoch=dataset.cfg.get(
                                          'steps_per_epoch_valid', None))

        valid_loader = DataLoader(
            valid_split,
            batch_size=cfg.val_batch_size,
            sampler=get_sampler(valid_sampler),
            num_workers=cfg.get('num_workers', 2),
            pin_memory=cfg.get('pin_memory', True),
            collate_fn=self.batcher.collate_fn,
            worker_init_fn=lambda x: np.random.seed(x + np.uint32(
                torch.utils.data.get_worker_info().seed)))

        self.optimizer, self.scheduler = model.get_optimizer(cfg)

        is_resume = model.cfg.get('is_resume', True)
        self.load_ckpt(model.cfg.ckpt_path, is_resume=is_resume)

        dataset_name = dataset.name if dataset is not None else ''
        tensorboard_dir = join(
            self.cfg.train_sum_dir,
            model.__class__.__name__ + '_' + dataset_name + '_torch')
        runid = get_runid(tensorboard_dir)
        self.tensorboard_dir = join(self.cfg.train_sum_dir,
                                    runid + '_' + Path(tensorboard_dir).name)

        writer = SummaryWriter(self.tensorboard_dir)
        self.save_config(writer)
        log.info("Writing summary in {}.".format(self.tensorboard_dir))
        record_summary = cfg.get('summary').get('record_for', [])

        log.info("Started training")

        for epoch in range(0, cfg.max_epoch + 1):

            log.info(f'=== EPOCH {epoch:d}/{cfg.max_epoch:d} ===')
            model.train()
            self.metric_train.reset()
            self.metric_val.reset()
            self.losses = []
            model.trans_point_sampler = train_sampler.get_point_sampler()
            self.summary = {'train': {}, 'valid': {}}

            for step, inputs in enumerate(tqdm(train_loader, desc='training')):
                if hasattr(inputs['data'], 'to'):
                    inputs['data'].to(device)
                self.optimizer.zero_grad()
                results = model(inputs['data'])
                loss, gt_labels, predict_scores = model.get_loss(
                    Loss, results, inputs, device)

                if predict_scores.size()[-1] == 0:
                    continue

                loss.backward()
                if model.cfg.get('grad_clip_norm', -1) > 0:
                    torch.nn.utils.clip_grad_value_(model.parameters(),
                                                    model.cfg.grad_clip_norm)
                self.optimizer.step()

                self.metric_train.update(predict_scores, gt_labels)

                self.losses.append(loss.cpu().item())
                # Save only for the first pcd in batch
                if 'train' in record_summary and step == 0:
                    self.summary['train'] = self.get_3d_summary(
                        results, inputs, epoch)

            self.scheduler.step()

            # --------------------- validation
            model.eval()
            self.valid_losses = []
            model.trans_point_sampler = valid_sampler.get_point_sampler()

            with torch.no_grad():
                for step, inputs in enumerate(
                        tqdm(valid_loader, desc='validation')):
                    if hasattr(inputs['data'], 'to'):
                        inputs['data'].to(device)

                    results = model(inputs['data'])
                    # gt_labels, predict_scores are always concatenated
                    # inputs['data'].point:
                    # model.batcher: empty or DefaultBatcher => fixed size point
                    # clouds
                    # KPConvBatcher: concatenated for KPConv, ...
                    # SparseConvUNetBatcher list[point] for SparseConvUNet
                    # PointTransformerbatcher rowsplits

                    loss, gt_labels, predict_scores = model.get_loss(
                        Loss, results, inputs, device)

                    if predict_scores.size()[-1] == 0:
                        continue

                    self.metric_val.update(predict_scores, gt_labels)

                    self.valid_losses.append(loss.cpu().item())
                    # Save only for the first batch
                    if 'valid' in record_summary and step == 0:
                        self.summary['valid'] = self.get_3d_summary(
                            results, inputs, epoch)

            self.save_logs(writer, epoch)

            if epoch % cfg.save_ckpt_freq == 0:
                self.save_ckpt(epoch)

    def get_batcher(self, device, split='training'):
        """Get the batcher to be used based on the device and split.
        """
        batcher_name = getattr(self.model.cfg, 'batcher')

        if batcher_name == 'DefaultBatcher':
            batcher = DefaultBatcher()
        elif batcher_name == 'ConcatBatcher':
            batcher = ConcatBatcher(device, self.model.cfg.name)
        else:
            batcher = None
        return batcher

    def get_3d_summary(self, results, inputs, epoch):
        """
        Create visualization for network inputs and outputs.

        Args:
            results (Tensor(B, N, C)): Prediction scores for all classes.
            inputs_batch: Batch of pointclouds and labels as a Dict with the
                fields:
                {
                'data' : { 'xyz': [(5,) Tensor(B,N,3)],
                    'labels': (B, N) }
                'attr' : {'idx': tensor (1,), 'name' : List pcd_name,
                    'path': List [file_paths],
                    'split': List ['train'|'test'|'valid']
                    }
                }

            epoch (int): step

        Returns:
            [Dict] visualizations of inputs and outputs suitable to save as an
                Open3D for TensorBoard summary.
        """
        # ipdb.set_trace()
        if not hasattr(self, "_first_step"):
            self._first_step = epoch
        label_to_names = self.dataset.get_label_to_names()
        if not hasattr(self.dataset, "name_to_labels"):
            self.dataset.name_to_labels = {
                name: label
                for label, name in self.dataset.get_label_to_names().items()
            }
        cfg = self.cfg.get('summary')
        max_pts = cfg.get('max_pts')
        if max_pts is None:
            max_pts = np.iinfo(np.int32).max
        use_reference = cfg.get('use_reference', False)
        max_outputs = cfg.get('max_outputs', 1)
        input_pcd = []
        gt_labels = []
        predict_labels = []

        def to_sum_fmt(tensor, dtype=np.int32):
            return tensor.cpu().detach().numpy().astype(dtype)

        if self._first_step == epoch or not use_reference:
            pointcloud = inputs['data']['xyz'][0]  # 0 => input to first layer
            pcd_subsample = np.linspace(0,
                                        pointcloud.shape[1] - 1,
                                        num=min(max_pts, pointcloud.shape[1]),
                                        dtype=int)
            input_pcd = to_sum_fmt(pointcloud[:max_outputs, pcd_subsample, :3],
                                   np.float32)
            gtl = inputs['data']['labels']
            gt_labels = np.atleast_3d(
                to_sum_fmt(gtl[:max_outputs, pcd_subsample]))
            predict_labels = np.atleast_3d(
                to_sum_fmt(
                    torch.argmax(results[:max_outputs, pcd_subsample, :], 2)))

        def get_reference_or(data_tensor):
            if self._first_step == epoch or not use_reference:
                return data_tensor
            return self._first_step

        summary_dict = {
            'semantic_segmentation': {
                "vertex_positions": get_reference_or(input_pcd),
                "vertex_gt_labels": get_reference_or(gt_labels),
                "vertex_predict_labels": predict_labels,
                'label_to_names': label_to_names
            }
        }
        return summary_dict

    def save_logs(self, writer, epoch):
        """Save logs from the training and send results to TensorBoard.
        """
        train_accs = self.metric_train.acc()
        val_accs = self.metric_val.acc()

        train_ious = self.metric_train.iou()
        val_ious = self.metric_val.iou()

        loss_dict = {
            'Training loss': np.mean(self.losses),
            'Validation loss': np.mean(self.valid_losses)
        }
        acc_dicts = [{
            'Training accuracy': acc,
            'Validation accuracy': val_acc
        } for acc, val_acc in zip(train_accs, val_accs)]

        iou_dicts = [{
            'Training IoU': iou,
            'Validation IoU': val_iou
        } for iou, val_iou in zip(train_ious, val_ious)]

        for key, val in loss_dict.items():
            writer.add_scalar(key, val, epoch)
        for key, val in acc_dicts[-1].items():
            writer.add_scalar("{}/ Overall".format(key), val, epoch)
        for key, val in iou_dicts[-1].items():
            writer.add_scalar("{}/ Overall".format(key), val, epoch)

        log.info(f"Loss train: {loss_dict['Training loss']:.3f} "
                 f" eval: {loss_dict['Validation loss']:.3f}")
        log.info(f"Mean acc train: {acc_dicts[-1]['Training accuracy']:.3f} "
                 f" eval: {acc_dicts[-1]['Validation accuracy']:.3f}")
        log.info(f"Mean IoU train: {iou_dicts[-1]['Training IoU']:.3f} "
                 f" eval: {iou_dicts[-1]['Validation IoU']:.3f}")

        for stage in self.summary:
            for key, summary_dict in self.summary[stage].items():
                label_to_names = summary_dict.pop('label_to_names', None)
                writer.add_3d('/'.join((stage, key)),
                              summary_dict,
                              epoch,
                              max_outputs=None,
                              label_to_names=label_to_names)

    def load_ckpt(self, ckpt_path=None, is_resume=True):
        """Load a checkpoint. You must pass the checkpoint and indicate if you want to resume."""
        train_ckpt_dir = join(self.cfg.logs_dir, 'checkpoint')
        make_dir(train_ckpt_dir)

        if ckpt_path is None:
            ckpt_path = latest_torch_ckpt(train_ckpt_dir)
            if ckpt_path is not None and is_resume:
                log.info('ckpt_path not given. Restore from the latest ckpt')
            else:
                log.info('Initializing from scratch.')
                return

        if not exists(ckpt_path):
            raise FileNotFoundError(f' ckpt {ckpt_path} not found')

        log.info(f'Loading checkpoint {ckpt_path}')
        ckpt = torch.load(ckpt_path, map_location=self.device)
        self.model.load_state_dict(ckpt['model_state_dict'])
        if 'optimizer_state_dict' in ckpt and hasattr(self, 'optimizer'):
            log.info(f'Loading checkpoint optimizer_state_dict')
            self.optimizer.load_state_dict(ckpt['optimizer_state_dict'])
        if 'scheduler_state_dict' in ckpt and hasattr(self, 'scheduler'):
            log.info(f'Loading checkpoint scheduler_state_dict')
            self.scheduler.load_state_dict(ckpt['scheduler_state_dict'])

    def save_ckpt(self, epoch):
        """Save a checkpoint at the passed epoch.
        """
        path_ckpt = join(self.cfg.logs_dir, 'checkpoint')
        make_dir(path_ckpt)
        torch.save(
            dict(epoch=epoch,
                 model_state_dict=self.model.state_dict(),
                 optimizer_state_dict=self.optimizer.state_dict(),
                 scheduler_state_dict=self.scheduler.state_dict()),
            join(path_ckpt, f'ckpt_{epoch:05d}.pth'))
        log.info(f'Epoch {epoch:3d}: save ckpt to {path_ckpt:s}')

    def save_config(self, writer):
        """Save experiment configuration with tensorboard summary."""
        if hasattr(self, 'cfg_tb'):
            writer.add_text("Description/Open3D-ML", self.cfg_tb['readme'], 0)
            writer.add_text("Description/Command line", self.cfg_tb['cmd_line'],
                            0)
            writer.add_text('Configuration/Dataset',
                            code2md(self.cfg_tb['dataset'], language='json'), 0)
            writer.add_text('Configuration/Model',
                            code2md(self.cfg_tb['model'], language='json'), 0)
            writer.add_text('Configuration/Pipeline',
                            code2md(self.cfg_tb['pipeline'], language='json'),
                            0)


PIPELINE._register_module(SemanticSegmentation, "torch")
