import argparse
import logging
import os
import random
import shutil
import sys
import time
from itertools import cycle

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from tensorboardX import SummaryWriter
from torch.nn import BCEWithLogitsLoss
from torch.nn.modules.loss import CrossEntropyLoss
from torch.utils.data import DataLoader
from torchvision import transforms,ops
from torchvision.utils import make_grid
from tqdm import tqdm
import datetime
from dataloaders import utils
from dataloaders.dataset_semi import (BaseDataSets, RandomGenerator,TwoStreamBatchSampler)
from networks.discriminator import FCDiscriminator
from networks.net_factory import net_factory
from utils import losses, metrics, ramps
from val_2D import test_single_volume2
from networks.vision_transformer import SwinUnet as ViT_seg
from config import get_config
from torch.nn import CosineSimilarity
from torch.utils.data.distributed import DistributedSampler
"""选择GPU ID"""
# gpu_list = [1,2] #[0,1]
# gpu_list_str = ','.join(map(str, gpu_list))
# os.environ.setdefault("CUDA_VISIBLE_DEVICES", gpu_list_str)
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

from utils.gate_crf_loss import ModelLossSemsegGatedCRF

parser = argparse.ArgumentParser()
parser.add_argument('--root_path', type=str,
                    default='/mnt/sdd/tb/data/ACDC', help='Name of Experiment')
parser.add_argument('--exp', type=str,
                    default='ACDC_Semi/Mean_Teacher', help='experiment_name')
parser.add_argument('--model', type=str,
                    default='unet_new', help='model_name')
parser.add_argument('--fold', type=str,
                    default='fold1', help='cross validation')
parser.add_argument('--sup_type', type=str,
                    default='scribble', help='supervision type')
parser.add_argument('--max_iterations', type=int,
                    default=30000, help='maximum epoch number to train')
parser.add_argument('--batch_size', type=int, default=40,
                    help='batch_size per gpu')
parser.add_argument('--deterministic', type=int,  default=1,
                    help='whether use deterministic training')
parser.add_argument('--base_lr', type=float,  default=0.01,
                    help='segmentation network learning rate')
parser.add_argument('--patch_size', type=list,  default=[256, 256],
                    help='patch size of network input')
parser.add_argument('--seed', type=int,  default=42, help='random seed')
parser.add_argument('--num_classes', type=int,  default=4,
                    help='output channel of network')

# label and unlabel
parser.add_argument('--labeled_bs', type=int, default=20,
                    help='labeled_batch_size per gpu')
parser.add_argument('--labeled_num', type=int, default=4,
                    help='labeled data')
# costs
parser.add_argument('--ema_decay', type=float,  default=0.99, help='ema_decay')
parser.add_argument('--ema_decay2', type=float,  default=0.8, help='ema_decay')
parser.add_argument('--consistency_type', type=str,
                    default="mse", help='consistency_type')
parser.add_argument('--consistency', type=float,
                    default=0.5, help='consistency')
parser.add_argument('--consistency_rampup', type=float,
                    default=200.0, help='consistency_rampup')

#trans parameters
parser.add_argument(
    '--cfg', type=str, default="/mnt/sdd/tb/WSL4MIS/code/configs/swin_tiny_patch4_window7_224_lite.yaml", help='path to config file', )
parser.add_argument(
    "--opts",
    help="Modify config options by adding 'KEY VALUE' pairs. ",
    default=None,
    nargs='+',
)
parser.add_argument('--zip', action='store_true',
                    help='use zipped dataset instead of folder dataset')
parser.add_argument('--cache-mode', type=str, default='part', choices=['no', 'full', 'part'],
                    help='no: no cache, '
                    'full: cache all data, '
                    'part: sharding the dataset into nonoverlapping pieces and only cache one piece')
parser.add_argument('--resume', help='resume from checkpoint')
parser.add_argument('--accumulation-steps', type=int,
                    help="gradient accumulation steps")
parser.add_argument('--use-checkpoint', action='store_true',
                    help="whether to use gradient checkpointing to save memory")
parser.add_argument('--amp-opt-level', type=str, default='O1', choices=['O0', 'O1', 'O2'],
                    help='mixed precision opt level, if O0, no amp is used')
parser.add_argument('--tag', help='tag of experiment')
parser.add_argument('--eval', action='store_true',
                    help='Perform evaluation only')
parser.add_argument('--throughput', action='store_true',
                    help='Test throughput only')

parser.add_argument('--my_lambda', type=float,  default=1, help='balance factor to control contrastive loss')
parser.add_argument('--tau', type=float,  default=1, help='temperature of the contrastive loss')

parser.add_argument("--local_rank", default=os.getenv('LOCAL_RANK', 2), type=int)
parser.add_argument("--kd_weights", type=int, default=15)

args = parser.parse_args()
config = get_config(args)
# 
device = torch.device('cuda:3' if torch.cuda.is_available() else 'cpu')

def get_current_consistency_weight(epoch):
    # Consistency ramp-up from https://arxiv.org/abs/1610.02242
    return args.consistency * ramps.sigmoid_rampup(epoch, args.consistency_rampup)


def update_ema_variables(model, ema_model, alpha, global_step):
    # Use the true average until the exponential average is more correct
    alpha = min(1 - 1 / (global_step + 1), alpha)
    for ema_param, param in zip(ema_model.parameters(), model.parameters()):
        ema_param.data.mul_(alpha).add_(1 - alpha, param.data)


def train(args, snapshot_path):


    base_lr = args.base_lr
    num_classes = args.num_classes
    batch_size = args.batch_size
    max_iterations = args.max_iterations

    def worker_init_fn(worker_id):
        random.seed(args.seed + worker_id)

    def create_model(ema=False):
        # Network definition
        # model = net_factory(net_type=args.model, in_chns=1,class_num=num_classes)
        model = ViT_seg(config, img_size=args.patch_size,num_classes=args.num_classes)      

        if ema:
            for param in model.parameters():
                param.detach_()
        return model
    
    model_aux = net_factory(net_type=args.model, in_chns=1,class_num=num_classes)
    model = create_model()
    ema_model = create_model(ema=True)


    model=model.to(device) 
    ema_model =ema_model.to(device)
    model_aux = model_aux.to(device)


    num_gpus = torch.cuda.device_count()
    
    db_train_labeled = BaseDataSets(base_dir=args.root_path, num=8, labeled_type="labeled", fold=args.fold, split="train", transform=transforms.Compose([
        RandomGenerator(args.patch_size)]),sup_type=args.sup_type)
    db_train_unlabeled = BaseDataSets(base_dir=args.root_path, num=8, labeled_type="unlabeled", fold=args.fold, split="train", transform=transforms.Compose([
        RandomGenerator(args.patch_size)]))



    trainloader_labeled = DataLoader(db_train_labeled, batch_size=args.batch_size//2, shuffle=True,
                                     num_workers=16, pin_memory=True, drop_last=True,worker_init_fn=worker_init_fn)
    trainloader_unlabeled = DataLoader(db_train_unlabeled, batch_size=args.batch_size//2, shuffle=True,
                                       num_workers=16, pin_memory=True, drop_last=True,worker_init_fn=worker_init_fn)

    db_val = BaseDataSets(base_dir=args.root_path,
                          fold=args.fold, split="val", )
    valloader = DataLoader(db_val, batch_size=1, shuffle=False,
                           num_workers=1)

    model.train()
    model_aux.train()

    optimizer2 = optim.SGD(model_aux.parameters(), lr=base_lr, momentum=0.9, weight_decay=0.0001)
    optimizer = optim.Adam(model.parameters(), lr=base_lr, weight_decay=0.0001) 

    ce_loss = CrossEntropyLoss(ignore_index=4)
    dice_loss = losses.pDLoss(num_classes, ignore_index=4)
    cos_sim = CosineSimilarity(dim=1,eps=1e-6)
    affinityenergyLoss=losses.SegformerAffinityEnergyLoss()
    criterion = torch.nn.MSELoss()


    gatecrf_loss = ModelLossSemsegGatedCRF()
    loss_gatedcrf_kernels_desc = [{"weight": 1, "xy": 6, "rgb": 0.1}]
    loss_gatedcrf_radius = 5


    writer = SummaryWriter(snapshot_path + '/log')
    logging.info("{} iterations per epoch".format(len(trainloader_labeled)))

    iter_num = 0
    max_epoch = max_iterations // len(trainloader_labeled) + 1
    best_performance = 0.0
    iterator = tqdm(range(max_epoch), ncols=70)
    for epoch_num in iterator:
        # train_sampler_labeled.set_epoch(epoch_num)
        for i, data in enumerate(zip(cycle(trainloader_labeled), trainloader_unlabeled)):
            sampled_batch_labeled, sampled_batch_unlabeled = data[0], data[1]

            volume_batch, label_batch = sampled_batch_labeled['image'], sampled_batch_labeled['label']
            label_batch_wr = sampled_batch_labeled['random_walker']
            crop_images = sampled_batch_labeled['crop_images']    
            boxes = sampled_batch_labeled['boxes']


            crop_images = crop_images.to(device)
            label_batch_wr = label_batch_wr.to(device)
            volume_batch, label_batch = volume_batch.to(device), label_batch.to(device)
            unlabeled_volume_batch = sampled_batch_unlabeled['image'].to(device)


            noise = torch.clamp(torch.randn_like(unlabeled_volume_batch) * 0.1, -0.2, 0.2)
            ema_inputs = unlabeled_volume_batch + noise
            # ema_inputs = torch.cat([volume_batch,ema_inputs],0)

            volume_batch=torch.cat([volume_batch,unlabeled_volume_batch],0)



            outputs,attpred = model(volume_batch)           
            outputs_unlabeled_soft = torch.softmax(outputs[args.labeled_bs:,...], dim=1)
            outputs_seg_soft = torch.softmax(outputs[:args.labeled_bs,...], dim=1)
            
            bs, bxs, c, h, w = crop_images.shape
            crop_images = crop_images.reshape(bs * bxs, c, h, w)

            feat_local,logits_local = model_aux(crop_images) 
            seg_soft_crop = torch.softmax(feat_local, dim=1)

            boxes = boxes.to(device).type_as(outputs)

            # visualize
            feat_local_label = feat_local.clone().detach()  # 4, 20, 224, 224
            
            # # normalize
            # ba = logits_local.shape[0]
            # feat_local_label[feat_local_label < 0] = 0
            # ll_max = torch.max(torch.max(feat_local_label, dim=3)[0], dim=2)[0]
            # feat_local_label = feat_local_label / (ll_max.unsqueeze(2).unsqueeze(3) + 1e-8)
            # for i in range(bs):
            #     ind = torch.nonzero(label_batch[i] == 0)
            #     feat_local_label[i * bxs:(i + 1) * bxs, ind] = 0

            # keep max value among all classes
            n, c, h, w = feat_local_label.shape
            feat_local_label_c = feat_local_label.permute(1, 0, 2, 3).reshape(c, -1)
            ind_f = torch.argsort(-feat_local_label_c, axis=0)
            pos = torch.eye(c)[ind_f[0]].transpose(0, 1).type_as(feat_local_label_c)
            feat_local_label_c = pos * feat_local_label_c
            feat_local_label = feat_local_label_c.reshape(c, n, h, w).permute(1, 0, 2, 3)


            # match the sal label    hyper-parameter
            feat_local_label = (feat_local_label > 0.35).type_as(feat_local_label)

 
            # roi align
            feat_aligned = []
            crop_label = []

            for i in range(n):
                feat_aligned_=ops.roi_align(outputs[:args.labeled_bs,...], boxes[i], (h, w), 1 / 8.0)
                feat_aligned.append(feat_aligned_.clone()[None])
                
                label_aligned_=ops.roi_align(label_batch.unsqueeze(1).type(torch.float32), boxes[i], (h, w), 1 / 8.0)

                crop_label.append(label_aligned_.clone()[None])  

            feat_aligned = torch.cat(feat_aligned, dim=0)  
            crop_label   = torch.cat(crop_label, dim=0)

            feat_aligned=feat_aligned.squeeze()
            crop_label  =crop_label.squeeze()


            # feat_aligned = ops.roi_align(outputs, boxes[-1], (h, w), 1 / 8.0)
            feat_aligned = F.softmax(feat_aligned, dim=1)
            loss_kd = criterion(feat_aligned, feat_local_label[:args.labeled_bs,...]) * args.kd_weights
            loss_ce_corp = ce_loss(feat_local,crop_label[:].long())* args.kd_weights


            loss_ce = ce_loss(outputs[:args.labeled_bs,...], label_batch[:].long())
            # loss_dice =ce_loss(outputs_seg[:args.labeled_bs,...], label_batch[:].long())

            # pseudo_labels = torch.zeros_like(ema_output)

            # channel_map = ema_attpred
            # threshold = torch.quantile(channel_map, 0.95) # Set the threshold for each channel
            # binary_mask = (channel_map > threshold).float()
            # pseudo_labels= binary_mask * outputs  

            # pseudo_labels = torch.argmax(pseudo_labels[:args.labeled_bs].detach(), dim=1, keepdim=False)


            loss_ce_wr = ce_loss(outputs[:args.labeled_bs,...], label_batch_wr[:].long())
            loss_dice_wr= dice_loss(outputs_seg_soft[:args.labeled_bs,...], label_batch_wr.unsqueeze(1))
            #dice_loss(outputs_soft[:args.labeled_bs,...], label_batch.unsqueeze(1))
            # supervised_loss = 0.5 * (loss_dice + loss_ce)
            supervised_loss=loss_ce  #+loss_dice_wr+loss_ce_wr


            pseudo_outputs1 = torch.argmax(outputs_seg_soft[args.labeled_bs:].detach(), dim=1, keepdim=False)

            pseudo_outputs2 = torch.argmax(seg_soft_crop[args.labeled_bs:].detach(), dim=1, keepdim=False)

            pseudo_supervision1 = dice_loss(outputs_seg_soft[args.labeled_bs:], pseudo_outputs2.unsqueeze(1))
            pseudo_supervision2 = dice_loss(seg_soft_crop[args.labeled_bs:], pseudo_outputs1.unsqueeze(1))


            # with torch.cuda.amp.autocast():
            #         # -1: unlabeled pixels (其中60%-70%是没有标注信息的)
            #         unlabeled_RoIs = (label_batch == 0)  
            #         label_batch[label_batch < 0] = 0
            #         affinity_loss = affinityenergyLoss(outputs, attpred, unlabeled_RoIs, label_batch)

            #         loss = supervised_loss + affinity_loss 

            # with torch.no_grad():
            #     ema_output,ema_attpred = ema_model(ema_inputs)
            #     ema_output_soft = torch.softmax(ema_output, dim=1)
                
            # #consistency loss
            # consistency_weight = get_current_consistency_weight(iter_num // 300)
            # if iter_num < 1000:
            #     consistency_loss = 0.0
            # else:
            #     consistency_loss = torch.mean((outputs_unlabeled_soft - ema_output_soft[args.labeled_bs:,...]) ** 2)
                    
            #aff_loss
            aff_loss = losses.get_aff_loss(attpred[:args.labeled_bs,...],label_batch_wr)

            loss = supervised_loss+aff_loss+loss_kd  #+affinity_loss+consistency_weight*consistency_loss
            optimizer.zero_grad()
            optimizer2.zero_grad()

            loss.backward()

            optimizer.step()
            optimizer2.step()
            optimizer.step()
            # update_ema_variables(model, ema_model, args.ema_decay, iter_num)


            lr_ = base_lr * (1.0 - iter_num / max_iterations) ** 0.9
            for param_group in optimizer.param_groups:
                param_group['lr'] = lr_

            iter_num = iter_num + 1
            writer.add_scalar('info/lr', lr_, iter_num)
            writer.add_scalar('info/total_loss', loss, iter_num)
            writer.add_scalar('info/loss_ce', loss_ce, iter_num)
            writer.add_scalar('info/loss_dice', loss_ce, iter_num)
            # writer.add_scalar('info/consistency_loss',consistency_loss, iter_num)
            # writer.add_scalar('info/consistency_weight',consistency_weight, iter_num)

            logging.info(
                'iteration %d : loss : %f, loss_ce: %f, loss_dice: %f' %
                (iter_num, loss.item(), loss_ce.item(), loss_ce.item()))

            if iter_num % 20 == 0:
                image = volume_batch[1, 0:1, :, :]
                writer.add_image('train/Image', image, iter_num)
                outputs = torch.argmax(torch.softmax(
                    outputs, dim=1), dim=1, keepdim=True)
                writer.add_image('train/Prediction',
                                 outputs[1, ...] * 50, iter_num)
                labs = label_batch[1, ...].unsqueeze(0) * 50
                writer.add_image('train/GroundTruth', labs, iter_num)

            if iter_num > 0 and iter_num % 200 == 0:
                model.eval()
                metric_list = 0.0
                for i_batch, sampled_batch in enumerate(valloader):
                    metric_i = test_single_volume2(
                        sampled_batch["image"].to(device), sampled_batch["label"].to(device), model, device=device,classes=num_classes)
                    metric_list += np.array(metric_i)
                metric_list = metric_list / len(db_val)
                for class_i in range(num_classes-1):
                    writer.add_scalar('info/val_{}_dice'.format(class_i+1),
                                      metric_list[class_i, 0], iter_num)
                    writer.add_scalar('info/val_{}_hd95'.format(class_i+1),
                                      metric_list[class_i, 1], iter_num)

                performance = np.mean(metric_list, axis=0)[0]

                mean_hd95 = np.mean(metric_list, axis=0)[1]
                writer.add_scalar('info/val_mean_dice', performance, iter_num)
                writer.add_scalar('info/val_mean_hd95', mean_hd95, iter_num)

                if performance > best_performance:
                    best_performance = performance
                    save_mode_path = os.path.join(snapshot_path,
                                                  'iter_{}_dice_{}.pth'.format(
                                                      iter_num, round(best_performance, 4)))
                    save_best = os.path.join(snapshot_path,
                                             '{}_best_model.pth'.format(args.model))
                    torch.save(model.state_dict(), save_mode_path)
                    torch.save(model.state_dict(), save_best)

                logging.info(
                    'iteration %d : mean_dice : %f mean_hd95 : %f' % (iter_num, performance, mean_hd95))
                model.train()

            if iter_num % 3000 == 0:
                save_mode_path = os.path.join(
                    snapshot_path, 'iter_' + str(iter_num) + '.pth')
                torch.save(model.state_dict(), save_mode_path)
                logging.info("save model to {}".format(save_mode_path))

            if iter_num >= max_iterations:
                break
        if iter_num >= max_iterations:
            iterator.close()
            break
    writer.close()
    return "Training Finished!"

def backup_code(base_dir):
    ###备份当前train代码文件及dataset代码文件
    code_path = os.path.join(base_dir, 'code') 
    if not os.path.exists(code_path):
        os.makedirs(code_path)
    train_name = os.path.basename(__file__)
    dataset_name = 'dataset_semi.py'
    # dataset_name2 = 'dataset_semi_weak_newnew_20.py'
    net_name1 = 'mix_transformer.py'  
    net_name2 = 'net_factory.py'  
    net_name3 = 'vision_transformer.py'
    shutil.copy('networks/' + net_name1, code_path + '/' + net_name1)
    shutil.copy('networks/' + net_name2, code_path + '/' + net_name2)
    shutil.copy('networks/' + net_name3, code_path + '/' + net_name3)
    shutil.copy('dataloaders/' + dataset_name, code_path + '/' + dataset_name)
    # shutil.copy('dataloaders/' + dataset_name2, code_path + '/' + dataset_name2)
    shutil.copy(train_name, code_path + '/' + train_name)

if __name__ == "__main__":
    if not args.deterministic:
        cudnn.benchmark = True
        cudnn.deterministic = False
    else:
        cudnn.benchmark = False
        cudnn.deterministic = True

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    snapshot_path = "/mnt/sdd/tb/work_dirs/model_tiaoshi/{}_{}/{}-{}".format(args.exp, args.fold, args.sup_type,datetime.datetime.now())
    if not os.path.exists(snapshot_path):
        os.makedirs(snapshot_path)
    # backup_code(snapshot_path)

    logging.basicConfig(filename=snapshot_path + "/log.txt", level=logging.INFO,
                        format='[%(asctime)s.%(msecs)03d] %(message)s', datefmt='%H:%M:%S')
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    logging.info(str(args))
    train(args, snapshot_path)
