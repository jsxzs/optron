import os, glob
import numpy as np
from natsort import natsorted

import torch
import torch.nn as nn
from torch import optim
from torchvision import transforms
from torch.utils.data import DataLoader

from models.ViTVNet import CONFIGS as CONFIGS_ViT
from models.ViTVNet import ViTVNet
from models.TransMorph import CONFIGS as CONFIGS_TM
from models.TransMorph import TransMorph
from models.VoxelMorph import VoxelMorph
from models.Optron import Optron

import utils.utils as utils
import utils.losses as losses
from utils.csv_logger import log_csv
from utils.train_utils import adjust_learning_rate, save_checkpoint

from data import datasets, trans

import argparse


'''
parse the command line arg
'''
parser = argparse.ArgumentParser()

parser.add_argument('--batch_size', type=int, default=1)

parser.add_argument('--train_dir', type=str, default='../autodl-tmp/IXI_data/Train/')
parser.add_argument('--val_dir', type=str, default='../autodl-tmp/IXI_data/Val/')
parser.add_argument('--label_dir', type=str, default='../LPBA40/label/')
parser.add_argument('--dataset', type=str, default='IXI')
parser.add_argument('--atlas_dir', type=str, default='../autodl-tmp/IXI_data/atlas.pkl')

parser.add_argument('--model', type=str, default='TransMorph')

parser.add_argument('--training_lr', type=float, default=1e-4)
parser.add_argument('--optron_lr', type=float, default=1e-1)
parser.add_argument('--epoch_start', type=int, default=0)
parser.add_argument('--max_epoch', type=int, default=500)

parser.add_argument('--optron_epoch', type=int, default=10, help='the number of iterations in optimization, 0 represents no optimization')

parser.add_argument('--weight_model', type=float, default=0.02)
parser.add_argument('--weight_opt', type=float, default=1)
parser.add_argument('--model_idx', type=int, default=-1, help='the index of model loaded')

args = parser.parse_args()


def load_model(img_size):
    if args.model == 'TransMorph':
        config = CONFIGS_TM['TransMorph']
        if args.dataset == 'LPBA':
            config.img_size = img_size
            config.window_size = (5, 6, 5, 5)
        model = TransMorph(config)
    elif args.model == 'VoxelMorph':
        model = VoxelMorph(img_size)
    elif args.model == 'ViTVNet':
        config = CONFIGS_ViT['ViT-V-Net']
        model = ViTVNet(config, img_size=img_size)
    model.cuda()
    
    return model


def load_data():
    if args.dataset == "IXI":
        train_composed = transforms.Compose([trans.RandomFlip(0), trans.NumpyType((np.float32, np.float32))])
        val_composed = transforms.Compose([trans.Seg_norm(), trans.NumpyType((np.float32, np.int16))])    
        train_set = datasets.IXIBrainDataset(glob.glob(args.train_dir + '*.pkl'), args.atlas_dir, transforms=train_composed)
        val_set = datasets.IXIBrainInferDataset(glob.glob(args.val_dir + '*.pkl'), args.atlas_dir, transforms=val_composed)
    elif args.dataset == "OASIS":
        train_composed = transforms.Compose([trans.NumpyType((np.float32, np.int16))])
        val_composed = transforms.Compose([trans.NumpyType((np.float32, np.int16))])
        train_set = datasets.OASISBrainDataset(glob.glob(args.train_dir + '*.pkl'), transforms=train_composed)
        val_set = datasets.OASISBrainInferDataset(glob.glob(args.val_dir + '*.pkl'), transforms=val_composed)
    elif args.dataset == "LPBA":
        train_composed = transforms.Compose([trans.RandomFlip(0), trans.NumpyType((np.float32, np.float32))])
        val_composed = transforms.Compose([trans.Seg_norm(), trans.NumpyType((np.float32, np.int16))])
        train_set = datasets.LPBADataset(glob.glob(args.train_dir + '*.nii.gz'), args.atlas_dir, transforms=train_composed)
        val_set = datasets.LPBAInferDataset(glob.glob(args.val_dir + '*.nii.gz'), args.atlas_dir, args.label_dir, transforms=val_composed)

    train_loader = DataLoader(train_set, batch_size=args.batch_size, shuffle=True, num_workers=0, pin_memory=False)
    val_loader = DataLoader(val_set, batch_size=1, shuffle=False, num_workers=0, pin_memory=False, drop_last=True)
    
    return train_loader, val_loader


def main():
    optron_epoch = args.optron_epoch # optimizer iteration
    epoch_start = args.epoch_start # start epoch (use for continue training)
    lr = args.training_lr # lr for model
    max_epoch = args.max_epoch 
    model_idx = args.model_idx
    
    weights_model = [1, args.weight_model] # loss weighs of model loss
    weights_opt = [1, args.weight_opt] # loss weights of optimizer

    save_dir = '{}_{}{}/'.format(args.model, args.dataset, '_opt' if optron_epoch else '')
    if not os.path.exists('checkpoints/'+save_dir):
        os.makedirs('checkpoints/'+save_dir)
    if not os.path.exists('logs/'+save_dir):
        os.makedirs('logs/'+save_dir)
    
    img_size = (160, 192, 160) if args.dataset == 'LPBA' else (160, 192, 224)
    
    '''
    initialize model
    '''
    model = load_model(img_size)

    '''
    initialize spatial transformation function
    '''
    reg_model = utils.register_model(img_size, 'nearest')
    reg_model.cuda()

    '''
    if continue from previous training
    '''
    if epoch_start:
        model_dir = 'checkpoints/' + save_dir
        updated_lr = round(lr * np.power(1 - (epoch_start) / max_epoch, 0.9), 8)
        best_model = torch.load(model_dir + natsorted(os.listdir(model_dir))[model_idx])['state_dict']
        print('Model: {} loaded!'.format(natsorted(os.listdir(model_dir))[model_idx]))
        model.load_state_dict(best_model)
    else:
        updated_lr = lr

    '''
    initialize dataset
    '''
    train_loader, val_loader = load_data()
    
    '''
    initialize optimizer and loss functions
    '''
    adam = optim.Adam(model.parameters(), lr=updated_lr, weight_decay=0, amsgrad=True)
    criterion_ncc = losses.NCC_vxm()
    criterion_reg = losses.Grad3d(penalty='l2')
    criterion_mse = nn.MSELoss()

    best_dsc = 0

    for epoch in range(epoch_start, max_epoch):
        print('Training Starts')
        '''
        training
        '''
        loss_all = utils.AverageMeter()
        idx = 0
        for data in train_loader:
            idx += 1
            model.train()
            adjust_learning_rate(adam, epoch, max_epoch, lr)
            data = [t.cuda() for t in data]
            x = data[0]
            y = data[1]
            x_in = torch.cat((x,y), dim=1)
            output = model(x_in)

            if optron_epoch:
                '''initialize Optron'''
                optron = Optron(img_size, output[1].clone().detach())
                optron_optimizer = optim.Adam(optron.parameters(), lr=args.optron_lr, weight_decay=0, amsgrad=True)
                adjust_learning_rate(optron_optimizer, epoch, max_epoch, args.optron_lr)

                for i in range(args.optron_epoch):
                    x_warped, optimized_flow = optron(x)
                    optron_loss_ncc = criterion_ncc(x_warped, y) * weights_opt[0]
                    optron_loss_reg = criterion_reg(optimized_flow, y) * weights_opt[1]
                    optron_loss = optron_loss_ncc + optron_loss_reg

                    optron_optimizer.zero_grad()
                    optron_loss.backward()
                    optron_optimizer.step()
                
                x_warped, optimized_flow = optron(x)
            
                loss_mse = criterion_mse(output[1], optimized_flow) * weights_model[0]
                loss_reg = criterion_reg(output[1], y) * weights_model[1]
                loss = loss_mse + loss_reg
                loss_vals = [loss_mse, loss_reg]
                loss_all.update(loss.item(), y.numel())
            else:
                loss_ncc = criterion_ncc(output[0], y)
                loss_reg = criterion_reg(output[1], y)
                loss = loss_ncc + loss_reg
                loss_vals = [loss_ncc, loss_reg]
                loss_all.update(loss.item(), y.numel())

            # compute gradient and do SGD step
            adam.zero_grad()
            loss.backward()
            adam.step()

            '''
            For OASIS dataset
            '''
            if args.dataset == "OASIS":
                y_in = torch.cat((y, x), dim=1)
                output = model(y_in)

                '''initialize Optron'''
                if optron_epoch:
                    optron = Optron(img_size, output[1].clone().detach())
                    optron_optimizer = optim.Adam(optron.parameters(), lr=args.optron_lr, weight_decay=0, amsgrad=True)
                    adjust_learning_rate(optron_optimizer, epoch, max_epoch, args.optron_lr)

                    for i in range(optron_epoch):
                        y_warped, optimized_flow = optron(y)
                        optron_loss_ncc = criterion_ncc(y_warped, x) * weights_opt[0]
                        optron_loss_reg = criterion_reg(optimized_flow, x) * weights_opt[1]
                        optron_loss = optron_loss_ncc + optron_loss_reg

                        optron_optimizer.zero_grad()
                        optron_loss.backward()
                        optron_optimizer.step()

                    loss_mse = criterion_mse(optimized_flow, output[1]) * weights_model[0]
                    loss_reg = criterion_reg(output[1], x) * weights_model[1]
                    loss = loss_mse + loss_reg
                    loss_vals = [loss_mse, loss_reg]
                else:
                    loss_ncc = criterion_ncc(output[0], x)
                    loss_reg = criterion_reg(output[1], x)
                    loss = loss_ncc + loss_reg
                    loss_vals = [loss_ncc, loss_reg]
                
                loss_all.update(loss.item(), x.numel())
                adam.zero_grad()
                loss.backward()
                adam.step()

            current_lr = adam.state_dict()['param_groups'][0]['lr']
            print('Epoch [{}/{}] Iter [{}/{}] - loss {:.4f}, Img Sim: {:.6f}, Reg: {:.6f}, lr: {:.6f}'.format(epoch, max_epoch, idx, len(train_loader), loss.item(), loss_vals[0].item(), loss_vals[1].item(), current_lr))


        '''
        validation
        '''
        eval_dsc = utils.AverageMeter()
        eval_det = utils.AverageMeter()
        with torch.no_grad():
            for data in val_loader:
                model.eval()
                data = [t.cuda() for t in data]
                x, y, x_seg, y_seg = data
                x_in = torch.cat((x, y), dim=1)
                output = model(x_in)
                def_out = reg_model([x_seg.cuda().float(), output[1].cuda()])

                '''update DSC'''
                if args.dataset == "OASIS":
                    dsc = utils.dice_OASIS(def_out.long(), y_seg.long())
                elif args.dataset == "IXI":
                    dsc = utils.dice_IXI(def_out.long(), y_seg.long())
                elif args.dataset == "LPBA":
                    dsc = utils.dice_LPBA(y_seg.cpu().detach().numpy(), def_out[0, 0, ...].cpu().detach().numpy())
                eval_dsc.update(dsc.item(), x.size(0))

                '''update Jdet'''
                jac_det = utils.jacobian_determinant_vxm(output[1].detach().cpu().numpy()[0, :, :, :, :])
                tar = y.detach().cpu().numpy()[0, 0, :, :, :]
                eval_det.update(np.sum(jac_det <= 0) / np.prod(tar.shape), x.size(0))

        '''save model'''
        best_dsc = max(eval_dsc.avg, best_dsc)
        save_checkpoint({
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'best_dsc': best_dsc,
            'optimizer': adam.state_dict(),
        }, save_dir='checkpoints/' + save_dir, 
        filename='dsc{:.3f}_epoch{:d}.pth.tar'.format(eval_dsc.avg, epoch))

        print('\nEpoch [{}/{}] - DSC: {:.6f}, Jdet: {:.8f}, loss: {:.6f}, lr: {:.6f}\n'.format(
            epoch, max_epoch, eval_dsc.avg, eval_det.avg, loss_all.avg, current_lr))
        log_csv(save_dir, epoch, eval_dsc.avg, eval_det.avg, loss_all.avg, current_lr)

        loss_all.reset()
        torch.cuda.empty_cache()


if __name__ == '__main__':
    main()
