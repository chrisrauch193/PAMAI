import numpy as np

from tqdm import tqdm
import shutil
import random

import torch
from torch import nn
from torch.backends import cudnn
from torch.autograd import Variable

#import model
from agents.base import BaseAgent
from graphs.models import Denet
from graphs.losses.example import BinaryCrossEntropy
from datasets.raw_audio import raw_audio_Dataloader
# import dataset

# import your classes here
from tensorboardX import SummaryWriter
from utils.metrics import AverageMeter, AverageMeterList, cls_accuracy
from utils.misc import print_cuda_statistics
from utils.train_utils import adjust_learning_rate
from utils.train_utils import get_net,get_loss,get_optimizer
cudnn.benchmark = True


class DenetAgent(BaseAgent):

    def __init__(self, config,wandb):
        super().__init__(config,wandb)

        # Converting context and shift in samples ??? input_dim = window-lengt, window shift is the shift 
        config.input_dim2=int(config.fs*config.cw_len/1000.00)
        config.wshift=int(config.fs*config.cw_shift/1000.00)

        # define models
        self.model = get_net(config)
        print(self.model)
        # Init experiment watcher
        self.wandb = wandb
        self.wandb.watch(self.model)
        # define data_loader
        self.data_loader = raw_audio_Dataloader(self.config)

        # define loss
        self.loss = get_loss(config.loss)

        # define optimizers for both generator and discriminator
        self.optimizer = get_optimizer(config,self.model)

        # initialize counter
        self.current_epoch = 0
        self.current_iteration = 0
        self.best_valid_acc = 0

        # set cuda flag
        self.is_cuda = torch.cuda.is_available()
        if self.is_cuda and not self.config.cuda:
           print("WARNING: You have a CUDA device, so you should probably enable CUDA")

        self.cuda = self.is_cuda & self.config.cuda
        
        # set the manual seed for torch
        self.manual_seed = self.config.seed
        if self.cuda:
            torch.cuda.manual_seed_all(self.manual_seed)
            torch.cuda.set_device(self.config.gpu_device)
            self.model = self.model.cuda()
            self.loss = self.loss.cuda()
            print("Program will run on *****GPU-CUDA***** ")
            print_cuda_statistics()
        else:
            print("Program will run on *****CPU*****\n")

        # Model Loading from the latest checkpoint if not found start from scratch.
        self.load_checkpoint(self.config.checkpoint_file)
        # Summary Writer, useless since I use wandb
        # self.summary_writer = SummaryWriter(log_dir=self.config.summary_dir, comment='FirstTest')

    def load_checkpoint(self, filename):
        """
        Latest checkpoint loader
        :param file_name: name of the checkpoint file
        :return:
        """
        filename = self.config.checkpoint_dir + filename
        try:
            print("Loading checkpoint '{}'".format(filename))
            checkpoint = torch.load(filename)

            self.current_epoch = checkpoint['epoch']
            self.current_iteration = checkpoint['iteration']
            self.model.load_state_dict(checkpoint['state_dict'])
            self.optimizer.load_state_dict(checkpoint['optimizer'])

            print("Checkpoint loaded successfully from '{}' at (epoch {}) at (iteration {})\n"
                             .format(self.config.checkpoint_dir, checkpoint['epoch'], checkpoint['iteration']))
        except OSError as e:
            print("No checkpoint exists from '{}'. Skipping...".format(self.config.checkpoint_dir))
            print("**First time to train**")

    def save_checkpoint(self, filename="checkpoint.pth.tar", is_best=0):
        """
        Checkpoint saver
        :param file_name: name of the checkpoint file
        :param is_best: boolean flag to indicate whether current checkpoint's accuracy is the best so far
        :return:
        """
        state = {
            'epoch': self.current_epoch,
            'iteration': self.current_iteration,
            'state_dict': self.model.state_dict(),
            'optimizer': self.optimizer.state_dict(),
        }
        # Save the state
        torch.save(state, self.config.checkpoint_dir + filename)
        # If it is the best copy it to another file 'model_best.pth.tar'
        if is_best:
            shutil.copyfile(self.config.checkpoint_dir + filename,
                            self.config.checkpoint_dir + 'model_best.pth.tar')

    def run(self):
        """
        The main operator
        :return:
        """
        try:
            # @TODO change mode test, not validate? Or just change dataloader
            # create test run
            if self.config.mode == 'test':
                self.validate()
            else:
                self.train()

        except KeyboardInterrupt:
            print("You have entered CTRL+C.. Wait to finalize")

    def train(self):
        """
        Main training loop
        :return:
        """
        for epoch in range(self.current_epoch, self.config.max_epoch):
            self.current_epoch = epoch
            self.train_one_epoch()

            if epoch%self.config.validate_every==0 or self.config.test_mode:
                valid_acc = self.validate()
                if self.config.test_mode:exit(1)
                is_best = valid_acc > self.best_valid_acc
                if is_best:
                    self.best_valid_acc = valid_acc
                self.save_checkpoint(is_best=is_best)
        


    def train_one_epoch(self):
        """
        One epoch of training
        uses tqdm to load data in parallel? Nope thats not true
        :return:
        """
        if self.config.test_mode: 
            self.data_loader.train_iterations = 10
        tqdm_batch = tqdm(self.data_loader.train_loader, total=self.data_loader.train_iterations,
                          desc="Epoch-{}-".format(self.current_epoch))
        # Set the model to be in training mode
        self.model.train()
        # Initialize your average meters @TODO: ARTHUR I still don't know what that is
        epoch_loss = AverageMeter()
        top1_acc = AverageMeter()
        top5_acc = AverageMeter()

        current_batch = 0
        for x, y in tqdm_batch:
            if self.cuda:
                x, y = x.cuda(non_blocking=self.config.async_loading), y.cuda(non_blocking=self.config.async_loading)
            x, y = Variable(x), Variable(y).unsqueeze(1).type(torch.float) # I don't even know why
            lr = adjust_learning_rate(self.optimizer, self.current_epoch, self.config, batch=current_batch,
                                      nBatch=self.data_loader.train_iterations)
            
            self.optimizer.zero_grad() 
            # model
            pred = self.model(x)
            # loss
            cur_loss = self.loss(pred, y)

            if np.isnan(float(cur_loss.item())):
                raise ValueError('Loss is nan during training...')
            # optimizer
            
            cur_loss.backward()
            self.optimizer.step()

            top1 = cls_accuracy(pred.data, y.data)

            epoch_loss.update(cur_loss.item())
            top1_acc.update(top1[0].item(), x.size(0))

            self.current_iteration += 1
            current_batch += 1

            self.wandb.log({"epoch/loss", epoch_loss.val})
            self.wandb.log({"epoch/accuracy", top1_acc.val})
        tqdm_batch.close()

        print("Training at epoch-" + str(self.current_epoch) + " | " + "loss: " + str(
            epoch_loss.val) + "- Top1 Acc: " + str(top1_acc.val) + "- Top5 Acc: " + str(top5_acc.val))


    def validate(self):
        """
        One cycle of model validation
        :return:
        """
        tqdm_batch = tqdm(self.data_loader.valid_loader, total=self.data_loader.valid_iterations,
                          desc="Valiation at -{}-".format(self.current_epoch))

        # set the model in training mode
        self.model.eval()

        epoch_loss = AverageMeter()
        top1_acc = AverageMeter()
        top5_acc = AverageMeter()

        for x, y in tqdm_batch:
            if self.cuda:
                x, y = x.cuda(non_blocking=self.config.async_loading), y.cuda(non_blocking=self.config.async_loading)

            x, y = Variable(x), Variable(y).unsqueeze(1).type(torch.float)
            # model
            pred = self.model(x)
            # loss
            cur_loss = self.loss(pred, y)
            if np.isnan(float(cur_loss.item())):
                raise ValueError('Loss is nan during validation...')

            top1 = cls_accuracy(pred.data, y.data)

            epoch_loss.update(cur_loss.item())
            top1_acc.update(top1[0].item(), x.size(0))


        print("Validation results at epoch-" + str(self.current_epoch) + " | " + "loss: " + str(
            epoch_loss.avg) + "- Top1 Acc: " + str(top1_acc.val) + "- Top5 Acc: " + str(top5_acc.val))

        tqdm_batch.close()

        return top1_acc.avg

    def finalize(self):
        """
        Finalize all the operations of the 2 Main classes of the process the operator and the data loader
        :return:
        """
        print("Please wait while finalizing the operation.. Thank you")
        self.save_checkpoint()
        # self.summary_writer.export_scalars_to_json("{}all_scalars.json".format(self.config.summary_dir))
        # self.summary_writer.close()
        # self.data_loader.finalize()