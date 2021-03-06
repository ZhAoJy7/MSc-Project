"""
A class for training encoder-decoder networks.
"""
from mnist import MNIST
import torch
import numpy as np
import copy
from scipy.interpolate import interpn
# from torch.utils.tensorboard import SummaryWriter
from torch.utils.tensorboard import SummaryWriter
import subprocess
import webbrowser
import tkinter as tk
from datetime import datetime
import math
import random
from torch.autograd import Variable
import matplotlib.pyplot as plt
import signal
import sys

from imednet.data.trajectory_loader import TrajectoryLoader
from imednet.utils.dmp_class import DMP
from imednet.utils.custom_optim import SCG, Adam




class Trainer:
    """
    Helper class containing methods for preparing data for training
    """
    train = False
    user_stop = ""
    plot_im = False
    indeks = []
    resetting_optimizer = False

    def __init__(self,
                 launch_tensorboard=False,
                 launch_gui=False,
                 plot_freq=0):
        self._launch_tensorboard = launch_tensorboard
        self._launch_gui = launch_gui
        self.plot_freq = plot_freq
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, sig, frame):
        """
        Handles signal interrupts by the user, e.g. Ctrl-C.
        """
        print('Training terminated by user!')
        self.cancel_training()

    def show_dmp(self, image, trajectory, dmp, plot=False, save=-1):
        """
        Plots and shows mnist image, trajectory and dmp to one picture

        image -> [HxW] or [H,W] or [H,W,C] shaped image.
        trajectory -> a trajectory containing all the points in format point = [x,y]
        dmp -> DMP created from the trajectory
        """
        C = None
        try:
            H, W, C = image.shape
        except:
            try:
                H, W = image.shape
            except:
                try:
                    image = image.squeeze()
                    H = W = int(np.sqrt(image.shape))
                    image = image.reshape(H,W)
                except Exception as e:
                    raise ValueError('Could not interpret image format!')

        fig = plt.figure()

        if image is not None:
            if C:
                plt.imshow(image, extent=[0, H+1, W+1, 0])
            else:
                plt.imshow(image, cmap='gray', extent=[0, H+1, W+1, 0])

        if dmp is not None:
            dmp.joint()
            # plt.plot(dmp.Y[:,0], dmp.Y[:,1],'-r', label='dmp', )
            # print("dmp.Y{}".format(dmp.Y))
            # print("dmp.Y len{}".format(dmp.Y.shape))
            # print("dmp.Y[:,0]{}".format(dmp.Y[:,0]))
            # print("dmp.Y[:,0] shape{}".format(dmp.Y[:,0].shape))
            # print("dmp.Y[:,1]{}".format(dmp.Y[:,1]))

            plt.plot(dmp.Y[:,0], dmp.Y[:,1],'-r', linewidth=3.0)
        if trajectory is not None:
            # plt.plot(trajectory[:,0], trajectory[:,1],'-b', label='trajectory')
            plt.plot(trajectory[:,0], trajectory[:,1],'-b', linewidth=3.0)
        # plt.legend()
        # plt.xlim([0,40])
        # plt.ylim([40,0])
        plt.axis('off')

        fig.canvas.draw()
        matrix = np.fromstring(fig.canvas.tostring_rgb(), dtype=np.uint8, sep='')
        matrix = matrix.reshape(fig.canvas.get_width_height()[::-1]+(3,))
        #if save != -1:
        #    plt.savefig("images/" + str(save) + ".pdf")
        #    plt.close(fig)
        #else:
        #    plt.show()
        if plot:
            plt.show()

        return fig, matrix

    def load_mnist_data(mnist_folder):
        """
        Loads data from the folder containing mnist files
        """
        mnistData = MNIST(mnist_folder)
        images, labels = mnistData.load_training()
        images = np.array(images)
        labels = np.array(labels)
        return images,labels

    def load_trajectories(trajectories_folder, available):
        """
        loads trajectories from the folder containing trajectory files
        """
        trajectories = []
        for i in available:
            t = TrajectoryLoader.loadNTrajectory(trajectories_folder,i)
            trajectories.append(t)
        trajectories = np.array(trajectories)
        return trajectories

    def create_dmps(trajectories,N, sampling_time):
        """
        Creates DMPs from the trajectorires

        trajectories -> list of trajectories to convert to DMPs
        N -> ampunt of base functions in the DMPs
        sampling_time -> sampling time for the DMPs
        """
        DMPs = []
        i = 0
        for trajectory in trajectories:
            dmp = DMP(N,sampling_time)
            x = trajectory[:,0]
            y = trajectory[:,1]
            time = np.array([trajectory[:,2],trajectory[:,2]]).transpose()[:-2]
            dt = np.diff(trajectory[:,2],1)
            if (dt == 0).any():
                print("Problem with ", i, " -th trajectory")
            dx = np.diff(x,1)/dt
            dy = np.diff(y,1)/dt
            ddy = np.diff(dy,1)/dt[:-1]
            ddx = np.diff(dx,1)/dt[:-1]
            path = np.array([i for i in zip(x,y)])[:-2]
            velocity = np.array([i for i in zip(dx,dy)])[:-1]
            acceleration = np.array([i for i in zip(ddx,ddy)])
            try:
                dmp.track(time, path, velocity, acceleration)
            except:
                print("Problem with ", i, " -th trajectory")
            DMPs.append(dmp)
            i += 1
        DMPs = np.array(DMPs)
        return DMPs

    def create_output_parameters(DMPs, scale = None):
        """
        Returns desired output parameters for the network from the given DMPs

        create_output_parameters(DMPs) -> parameters for each DMP in form [tau, y0, dy0, goal, w]
        DMPs -> list of DMPs that pair with the images input to the network
        """
        outputs = []
        for dmp in DMPs:
            learn = np.append(dmp.tau[0], dmp.y0)
            learn = np.append(learn, dmp.dy0)
            learn = np.append(learn, dmp.goal)
            learn = np.append(learn, dmp.w)
            outputs.append(learn)
        outputs = np.array(outputs)
        if scale is None:
            scale = np.array([np.abs(outputs[:,i]).max() for i in range(outputs.shape[1])])
            scale[7:] = scale[7:].max()
        outputs = outputs / scale
        return outputs, scale

    def get_data_for_network(images,DMPs, scale = None, useData = None):
        """
        Generates data that will be given to the Network

        get_data_for_network(images,DMPs,i,j) -> (input_data,output_data) for the Network
        images -> MNIST images that will be fed to the Network
        DMPs -> DMPs that pair with MNIST images given in the same order
        useData -> array like containing indexes of images to use
        """
        if useData is not None:
            input_data = Variable(torch.from_numpy(images[useData])).float()
        else:
            input_data = Variable(torch.from_numpy(images)).float()
        input_data = input_data/128 - 1
        if DMPs is not None:
            if scale is None:
                outputs, scale = Trainer.create_output_parameters(DMPs)
            else:
                outputs, scale = Trainer.create_output_parameters(DMPs, scale)
            output_data = Variable(torch.from_numpy(outputs),requires_grad= False).float()
        else:
            output_data = None
            scale = 1
        return input_data, output_data, scale
    

    def get_dmp_from_image(self, network,image, N, sampling_time, cuda = False):
        if cuda:
          image = image.cuda()
        output = network(image)
        dmps = []
        if len(image.size()) == 1:
            dmps.append(Trainer.create_dmp(output, network.scale,sampling_time,N, cuda))
        else:
            for data in output:
                dmps.append(Trainer.create_dmp(data, network.scale,sampling_time,N, cuda))
        return dmps

    def create_dmp(self, output, scale, sampling_time, N, cuda = False):
        if cuda:
          output = output.cpu()

        output = (scale.x_max-scale.x_min) * (output.double().data.numpy() -scale.y_min) / (scale.y_max-scale.y_min) + scale.x_min

        tau = output[0]
        # print("tau{}".format(tau))
        y0 = output[1:3]
        # print("y0{}".format(y0))
        goal = output[3:5]
        # print("goal{}".format(goal))
        weights = output[5:]
        # print("weights{}".format(weights))
        w = weights.reshape(N,2)
        # print("w{}".format(w))

        #y0 = output[0:2]
        #dy0 = 0*output[0:2]
        #goal = output[2:4]
        #weights = output[4:]
        dmp = DMP(N, sampling_time)
        dmp.values(N, sampling_time, tau, y0, [0, 0], goal, w)
        return dmp

    def show_network_output(network, i, images, trajectories, DMPs, N, sampling_time, available=None, cuda=False):
        input_data, output_data, scale = Trainer.get_data_for_network(images, DMPs, available)
        scale = network.scale
        if i != -1:
            input_data = input_data[i]
        dmps = Trainer.get_dmp_from_image(network, input_data, N, sampling_time, cuda)
        for dmp in dmps:
            dmp.joint()

        if i != -1:
            print('Dmp from network:')
            Trainer.print_dmp_data(dmps[0])
            print()
        if DMPs is not None and i != -1:
            print('Original DMP from trajectory:')
            Trainer.print_dmp_data(DMPs[i])
        if i == -1:
            plt.ion()
            for i in range(len(dmps)):
                Trainer.show_dmp(images[i], None, dmps[i])
            plt.ioff()
        else:
            if available is not None:
                Trainer.show_dmp(images[available[i]], trajectories[i], dmps[0])
            elif trajectories is not None:
                Trainer.show_dmp(images[i], trajectories[i], dmps[0])
            else:
                Trainer.show_dmp(images[i], None, dmps[0])

    def print_dmp_data(self, dmp):
        print('Tau: ', dmp.tau)
        print('y0: ', dmp.y0)
        print('dy0: ', dmp.dy0)
        print('goal: ', dmp.goal)
        print('w_sum: ', dmp.w.sum())

    def create_rotation_matrix(theta, dimensions=3):
        c, s = np.cos(theta), np.sin(theta)
        if dimensions == 3:
            return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        else:
            return np.array([[c, -s], [s, c]])

    def translate(points, movement):
        pivotPoint = np.array(movement)
        new_points = np.array(points) + pivotPoint
        return new_points

    def rotate_traj(trajectory, pivotPoint, theta):
        pivotPoint = np.append(pivotPoint, 0)
        transformed_trajectory = Trainer.translate(trajectory, -pivotPoint)
        transformed_trajectory = Trainer.create_rotation_matrix(theta).dot(transformed_trajectory.transpose()).transpose()
        transformed_trajectory = Trainer.translate(transformed_trajectory, pivotPoint)
        return transformed_trajectory

    def rotate_image(image, theta):
        new_image = image.reshape(28, 28)
        points =  np.array([[j, i, 0] for j in np.arange(28) for i in range(28)])
        transformed = Trainer.rotate_traj(points, [12,12], theta)[:,:2]
        t = (points[:28,1], points[:28,1])
        return interpn(t, image.reshape(28,28), transformed, method = 'linear', bounds_error=False, fill_value=0)

    def randomly_rotate_data(trajectories, images, n):
        transformed_trajectories = []
        transformed_images =[]
        for i in range(len(trajectories)):
            trajectory = trajectories[i]
            image = images[i]
            transformed_images.append(image)
            transformed_trajectories.append(trajectory)
            for j in range(n):
                theta = (np.random.rand(1)*np.pi/9)[0]
                new_trajectory = Trainer.rotateAround(trajectory, [12,12], theta)
                new_image = Trainer.rotate_image(image, theta)
                transformed_images.append(new_image)
                transformed_trajectories.append(new_trajectory)
        transformed_trajectories = np.array(transformed_trajectories)
        transformed_images = np.array(transformed_images)
        return transformed_trajectories, transformed_images

    # def test_on_image(file, model):
    #     image = plt.imread(file)
    #     transformed = np.zeros([28, 28])
    #     for i in range(28):
    #         for j in range(28):
    #             transformed[i, j] = image[i, j].sum()/3
    #     transformed /= transformed.max()
    #     plt.figure()
    #     plt.imshow(image)
    #     plt.figure()
    #     plt.imshow(transformed, cmap='gray')
    #     plt.show()
    #     Trainer.show_network_output(model, 0, np.array([transformed.reshape(784)*255]), None, None, N, sampling_time, cuda = cuda)

    def split_dataset(self, images, outputs, train_set = 0.7, validation_set = 0.15, test_set = 0.15):
        r = len(images)
        de = int(len(outputs)/r)
        trl = round(r*train_set)
        tel = round(r*test_set)
        val = r - trl - tel

        indeks = np.append(np.zeros(trl), np.ones(tel))
        indeks = np.append(indeks, 2*np.ones(val))

        random.shuffle(indeks)
        x_t = []
        y_t = []
        x_v = []
        y_v = []
        x_te = []
        y_te = []

        if self.indeks != []:
            indeks = self.indeks
        else:
            self.indeks = indeks

        for i in range(0, len(indeks)):
            if indeks[i] == 0:
                x_t.append(images[i])
                y_t.append(outputs[i * de])

                if de > 1:
                    y_t.append(outputs[i * de+1])

            if indeks[i] == 2:
                x_v.append(images[i])
                y_v.append(outputs[i*de])
                if de > 1:
                    y_v.append(outputs[i * de+1])

            if indeks[i] == 1:
                x_te.append(images[i])
                y_te.append(outputs[i*de])
                if de > 1:
                    y_te.append(outputs[i * de+1])

        x_train = np.array(x_t)
        y_train = np.array(y_t)
        x_validate = np.array(x_v)
        y_validate = np.array(y_v)
        x_test = np.array(x_te)
        y_test = np.array(y_te)

        input_data_train = Variable(torch.from_numpy(x_train)).float()
        output_data_train = Variable(torch.from_numpy(y_train), requires_grad=False).float()
        input_data_test = Variable(torch.from_numpy(x_test)).float()
        output_data_test = Variable(torch.from_numpy(y_test), requires_grad=False).float()
        input_data_validate = Variable(torch.from_numpy(x_validate)).float()
        output_data_validate = Variable(torch.from_numpy(y_validate), requires_grad=False).float()

        return input_data_train, output_data_train, input_data_test, output_data_test, input_data_validate, output_data_validate

    def train(self, model, images, outputs, path, train_param, file,
              optimizer_type='SCG', learning_rate=None,
              momentum=None, lr_decay=None, weight_decay=None):
        """
        Trains the network using provided data

        x -> input for the Network
        y -> desired output of the network for given x
        epochs -> how many times to repeat learning_rate
        learning_rate -> how much the weight will be changed each epoch
        log_interval -> on each epoch divided by log_interval log will be printed
        """
        # Launch GUI
        if self._launch_gui:
            root = tk.Tk()
            button = tk.Button(root,
                               text="QUIT",
                               fg="red",
                               command=self.cancel_training)
            button1 = tk.Button(root,
                               text="plot",
                               fg="blue",
                               command=self.plot_image)
            buttonResetAdam = tk.Button(root,
                                text="reset ADAM",
                                fg="blue",
                                command=self.reset_ADAM)
            button.pack(side=tk.LEFT)
            button1.pack(side=tk.RIGHT)
            buttonResetAdam.pack(side=tk.RIGHT)

        # Prepare parameters
        starting_time = datetime.now()
        train_param.data_samples = len(images)
        val_count = 0
        old_time_d = 0
        oldLoss = 0
        saving_epochs = 0

        file.write(train_param.write_out())
        print('Starting training')
        print(train_param.write_out())

        # Train
        writer = SummaryWriter(path+'/log')

        if self._launch_tensorboard:
            command = ["tensorboard", "--logdir=" + path+"/log"]
            tensorboard_process = subprocess.Popen(command)
            print('Launching tensorboard with process id: {}'.format(tensorboard_process.pid))

        # Divide data
        print("Dividing data")
        input_data_train_b, output_data_train_b, input_data_test_b, output_data_test_b, input_data_validate_b, output_data_validate_b = self.split_dataset(images, outputs)

        # dummy = model(torch.autograd.Variable(torch.rand(1,1600)))
        # writer.add_graph(model, dummy)

        if self._launch_tensorboard:
            window = webbrowser.open_new('http://localhost:6006')

        if train_param.cuda:
            torch.cuda.set_device(train_param.device)
            model = model.cuda()
            input_data_train_b = input_data_train_b.cuda()
            output_data_train_b = output_data_train_b.cuda()
            input_data_test_b = input_data_test_b.cuda()
            output_data_test_b = output_data_test_b.cuda()
            input_data_validate_b = input_data_validate_b.cuda()
            output_data_validate_b = output_data_validate_b.cuda()

        print('finish dividing')

        criterion = torch.nn.MSELoss(size_average=True) #For calculating loss (mean squared error)

        # scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, 200)

        # Set up optimizer
        if optimizer_type.lower() == 'customadam':
            if learning_rate:
                optimizer = Adam(model.parameters(), lr=learning_rate, amsgrad=True)
            else:
                optimizer = Adam(model.parameters(), amsgrad=True)
        elif optimizer_type.lower() == 'adam':
            if learning_rate and weight_decay:
                optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=weight_decay, eps=0.001)
            elif learning_rate:
                optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, eps=0.001)
            else:
                optimizer = torch.optim.Adam(model.parameters(), eps=0.001)
        elif optimizer_type.lower() == 'sgd':
            if learning_rate and momentum:
                optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate, momentum=momentum)
            elif learning_rate:
                optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate)
            elif momentum:
                optimizer = torch.optim.SGD(model.parameters(), momentum=momentum)
            else:
                optimizer = torch.optim.SGD(model.parameters())
        elif optimizer_type.lower() == 'adagrad':
            if learning_rate and lr_decay and weight_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr=learning_rate, lr_decay=lr_decay, weight_decay=weight_decay)
            elif learning_rate and lr_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr=learning_rate, lr_decay=lr_decay)
            elif learning_rate and weight_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
            elif learning_rate:
                optimizer = torch.optim.Adagrad(model.parameters(), lr=learning_rate)
            elif lr_decay and weight_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr_decay=lr_decay, weight_decay=weight_decay)
            elif lr_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr_decay=lr_decay)
            elif lr_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr_decay=lr_decay)
            else:
                optimizer = torch.optim.Adagrad(model.parameters())
        elif optimizer_type.lower() == 'rmsprop':
            optimizer = torch.optim.RMSprop(model.parameters())
        else:
            optimizer = SCG(filter(lambda p: p.requires_grad, model.parameters()))

        y_val = model(input_data_validate_b)
        oldValLoss = criterion(y_val, output_data_validate_b[:, 1:55]).data.item()
        bestValLoss = oldValLoss
        best_nn_parameters = copy.deepcopy(model.state_dict())
        # Infinite epochs
        if train_param.epochs == -1:
            inf_k = 0
        else:
            inf_k = 1

        self.train = True

        t = 0

        t_init = 200
        lr = 0

        while self.train:
            input_data_train = input_data_train_b.clone()
            output_data_train = output_data_train_b.clone()
            input_data_test = input_data_test_b.clone()
            output_data_test = output_data_test_b.clone()
            input_data_validate = input_data_validate_b.clone()
            output_data_validate = output_data_validate_b.clone()

            if self._launch_gui:
                root.update()

            if t > 0 and self.plot_freq != 0 and t % self.plot_freq == 0:
                self.plot_im = True

            t = t+1
            i = 0
            j = train_param.batch_size

            # scheduler.step()
            # if t%t_init == 0:
            #     scheduler.last_epoch = -1

            # writer.add_scalar('data/learning_rate', scheduler.get_lr()[0], t)

            self.loss = Variable(torch.Tensor([0]))
            permutations = torch.randperm(len(input_data_train))
            if model.isCuda():
                permutations = permutations.cuda()
                self.loss = self.loss.cuda()
            input_data_train = input_data_train[permutations]
            output_data_train = output_data_train[permutations]
            ena = []
            while j <= len(input_data_train):
                self.train_one_step(model,input_data_train[i:j], output_data_train[i:j, 1:55], learning_rate, criterion, optimizer)
                i = j
                j += train_param.batch_size

                '''for group in optimizer.param_groups:
                    i = 0
                    for p in group['params']:
                        i = i+1
                        if i ==15:

                            r1 = p.data[0][0]'''

            if i < len(input_data_train):
                self.train_one_step(model,input_data_train[i:], output_data_train[i:, 1:], learning_rate, criterion, optimizer)

            if (t-1)%train_param.log_interval ==0:
                self.loss = self.loss * train_param.batch_size / len(input_data_train)

                if t == 1:
                    oldLoss = self.loss

                print('Epoch: ', t, ' loss: ', self.loss.data[0])
                time_d = datetime.now()-starting_time
                writer.add_scalar('data/time', t, time_d.total_seconds())
                writer.add_scalar('data/training_loss', math.log( self.loss), t)
                writer.add_scalar('data/epochs_speed', 60*train_param.log_interval/(time_d.total_seconds()-old_time_d), t)
                writer.add_scalar('data/gradient_of_performance', (self.loss-oldLoss)/train_param.log_interval, t)
                old_time_d = time_d.total_seconds()
                oldLoss = self.loss


            if (t-1)%train_param.validation_interval == 0:
                y_val = model(input_data_validate)
                val_loss = criterion(y_val, output_data_validate[:, 1:55])
                writer.add_scalar('data/val_loss', math.log(val_loss), t)

                if val_loss.data.item() < bestValLoss:
                    bestValLoss = val_loss.data.item()
                    best_nn_parameters = copy.deepcopy(model.state_dict())
                    saving_epochs = t
                    torch.save(model.state_dict(), path + '/net_parameters')

                if val_loss.data.item() > bestValLoss:  # oldValLoss:
                    val_count = val_count+1
                else:
                    val_count = 0

                oldValLoss = val_loss.data.item()
                writer.add_scalar('data/val_count', val_count, t)
                print('Validation: ', t, ' loss: ', val_loss.data.item(), ' best loss:', bestValLoss)

                if (t - 1) % 10 == 0:
                    state = model.state_dict()
                    mean_dict = dict()
                    max_dict = dict()
                    min_dict = dict()
                    var_dict = dict()
                    for group in state:
                        mean = torch.mean(state[group])
                        max = torch.max(state[group])
                        min = torch.min(state[group])
                        var = torch.var(state[group])
                        mean_dict[group] = mean
                        max_dict[group] = max
                        min_dict[group] = min
                        var_dict[group] = var

                    writer.add_scalars('data/mean', mean_dict, t)
                    writer.add_scalars('data/max', max_dict, t)
                    writer.add_scalars('data/min', min_dict, t)
                    writer.add_scalars('data/var', var_dict, t)

                if self.plot_im:

                    # Try plotting spatial transformer network (STN) output
                    # if model contains an STN module (e.g. STIMEDNet)
                    try:
                        plt.subplot(211)
                        stn_val_image, stn_val_theta = model.stn(input_data_validate[0].reshape(-1,1,40,40))
                        plt.imshow(np.reshape(stn_val_image.data[0].cpu().numpy(), (40, 40)), cmap='gray', extent=[0, 40, 40, 0])
                        plt.subplot(212)
                    except:
                        pass

                    plot_vector = torch.cat((output_data_validate[0,0:1], y_val[0, :]), 0)
                    dmp_v = self.create_dmp(plot_vector, model.scale, 0.01, 25, True)
                    dmp = self.create_dmp(output_data_validate[0,:], model.scale, 0.01, 25, True)
                    dmp.joint()
                    dmp_v.joint()
                    _,mat = self.show_dmp((input_data_validate.data[0]).cpu().numpy(), dmp.Y , dmp_v, plot=False)
                    a = output_data_validate[0, :]
                    writer.add_image('image'+str(t), mat)
                    self.plot_im = False

                    # torch.save(model.state_dict(), path + '/net_parameters' +str(t))

            if (t - 1) % train_param.test_interval == 0:
                y_test = model(input_data_test)
                test_loss = criterion(y_test, output_data_test[:, 1:55])
                writer.add_scalar('data/test_loss', math.log(test_loss), t)

            '''if (t-1) % 1500 == 0:
                optimizer.reset = True
                print('reset optimizer')
            '''
            if self.resetting_optimizer:
                optimizer.reset = True

            if val_count == 7 or (t - 1) % 500 == 0:

                train_param.stop_criterion = "reset optimizer"
                print('periodic optimizer reset')
                optimizer.reset = True

            #End condition
            if inf_k*t > inf_k*train_param.epochs:
                self.train = False
                train_param.stop_criterion = "max epochs reached"

            if val_count > train_param.val_fail:
                self.train = False
                train_param.stop_criterion = "max validation fail reached"

            '''
            writer.add_scalar('data/test_lr', lr, t)


            writer.add_scalar('data/loss_lr', self.loss.data[0], lr)

            lr = 1.1**(t/40)-1
            print(lr)
            for param_group in optimizer.param_groups:
                param_group['lr'] = lr
            '''

        train_param.real_epochs = t
        train_param.min_train_loss = self.loss.data[0]
        train_param.min_val_loss = bestValLoss
        train_param.min_test_loss = test_loss
        train_param.elapsed_time = time_d.total_seconds()
        train_param.val_count = val_count
        k = (self.loss-oldLoss)/train_param.log_interval
        train_param.min_grad = k.data[0]
        train_param.stop_criterion = train_param.stop_criterion + self.user_stop

        file.write('\n'+str(optimizer))
        file.write('\n'+str(criterion))
        file.write('\n saving_epochs = ' + str(saving_epochs))
        file.write(train_param.write_out_after())
        writer.close()

        if self._launch_tensorboard:
            print('Terminating tensorboard with process id: {}'.format(tensorboard_process.pid))
            tensorboard_process.terminate()

        print('Training finished\n')

        return best_nn_parameters

    def train_dmp(self, model, images, outputs, path, train_param, file,
                  optimizer_type='SCG', learning_rate=None, momentum=None,
                  lr_decay=None, weight_decay=None):
        """
        teaches the network using provided data

        x -> input for the Network
        y -> desired output of the network for given x
        epochs -> how many times to repeat learning_rate
        learning_rate -> how much the weight will be changed each epoch
        log_interval -> on each epoch divided by log_interval log will be printed
        """
        # Launch GUI
        if self._launch_gui:
            root = tk.Tk()
            button = tk.Button(root,
                               text="QUIT",
                               fg="red",
                               command=self.cancel_training)
            button1 = tk.Button(root,
                                text="plot",
                                fg="blue",
                                command=self.plot_image)
            buttonResetAdam = tk.Button(root,
                                        text="reset ADAM",
                                        fg="blue",
                                        command=self.reset_ADAM)
            button.pack(side=tk.LEFT)
            button1.pack(side=tk.RIGHT)
            buttonResetAdam.pack(side=tk.RIGHT)

        # prepare parameters
        starting_time = datetime.now()
        train_param.data_samples = len(images)
        val_count = 0
        old_time_d = 0
        oldLoss = 0
        saving_epochs = 0

        file.write(train_param.write_out())
        print('Starting training')
        print(train_param.write_out())

        # Train

        writer = SummaryWriter(path + '/log')

        if self._launch_tensorboard:
            command = ["tensorboard", "--logdir=" + path + "/log"]
            tensorboard_process = subprocess.Popen(command)
            print('Launching tensorboard with process id: {}'.format(tensorboard_process.pid))

        # Divide data
        print("Dividing data")
        input_data_train_b, output_data_train_b, input_data_test_b, output_data_test_b, input_data_validate_b, output_data_validate_b = self.split_dataset(
            images, outputs)

        # dummy = model(torch.autograd.Variable(torch.rand(1,1600)))
        # writer.add_graph(model, dummy)

        if self._launch_tensorboard:
            window = webbrowser.open_new('http://localhost:6006')

        if train_param.cuda:
            torch.cuda.set_device(train_param.device)
            model = model.cuda()
            input_data_train_b = input_data_train_b.cuda()
            output_data_train_b = output_data_train_b.cuda()
            input_data_test_b = input_data_test_b.cuda()
            output_data_test_b = output_data_test_b.cuda()
            input_data_validate_b = input_data_validate_b.cuda()
            output_data_validate_b = output_data_validate_b.cuda()

        print('finish dividing')

        criterion = torch.nn.MSELoss(size_average=True)  # For calculating loss (mean squared error)
        # criterion=torch.nn.CrossEntropyLoss(size_average=True)

        # Set up optimizer
        if optimizer_type.lower() == 'customadam':
            if learning_rate:
                optimizer = Adam(model.parameters(), lr=learning_rate, amsgrad=True)
            else:
                optimizer = Adam(model.parameters(), amsgrad=True)
        elif optimizer_type.lower() == 'adam':
            if learning_rate and weight_decay:
                optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=weight_decay, eps=0.001)
            elif learning_rate:
                optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, eps=0.001)
            else:
                optimizer = torch.optim.Adam(model.parameters(), eps=0.001)
        elif optimizer_type.lower() == 'sgd':
            if learning_rate and momentum:
                optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate, momentum=momentum)
            elif learning_rate:
                optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate)
            elif momentum:
                optimizer = torch.optim.SGD(model.parameters(), momentum=momentum)
            else:
                optimizer = torch.optim.SGD(model.parameters())
        elif optimizer_type.lower() == 'adagrad':
            if learning_rate and lr_decay and weight_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr=learning_rate, lr_decay=lr_decay, weight_decay=weight_decay)
            elif learning_rate and lr_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr=learning_rate, lr_decay=lr_decay)
            elif learning_rate and weight_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
            elif learning_rate:
                optimizer = torch.optim.Adagrad(model.parameters(), lr=learning_rate)
            elif lr_decay and weight_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr_decay=lr_decay, weight_decay=weight_decay)
            elif lr_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr_decay=lr_decay)
            elif lr_decay:
                optimizer = torch.optim.Adagrad(model.parameters(), lr_decay=lr_decay)
            else:
                optimizer = torch.optim.Adagrad(model.parameters())
        elif optimizer_type.lower() == 'rmsprop':
            optimizer = torch.optim.RMSprop(model.parameters())
        else:
            optimizer = SCG(filter(lambda p: p.requires_grad, model.parameters()))

        y_val = model(input_data_validate_b)
        oldValLoss = criterion(y_val, output_data_validate_b[:, 1:55])
        bestValLoss = oldValLoss
        best_nn_parameters = copy.deepcopy(model.state_dict())

        # Infinite epochs
        if train_param.epochs == -1:
            inf_k = 0
        else:
            inf_k = 1

        self.train = True

        t = 0

        t_init = 200
        lr = 0

        while self.train:
            input_data_train = input_data_train_b.clone()
            output_data_train = output_data_train_b.clone()
            input_data_test = input_data_test_b.clone()
            output_data_test = output_data_test_b.clone()
            input_data_validate = input_data_validate_b.clone()
            output_data_validate = output_data_validate_b.clone()

            if self._launch_gui:
                root.update()

            if t > 0 and self.plot_freq != 0 and t % self.plot_freq == 0:
                self.plot_im = True

            t = t + 1
            i = 0
            j = train_param.batch_size

            # scheduler.step()
            # if t%t_init == 0:
            #    scheduler.last_epoch = -1

            # writer.add_scalar('data/learning_rate', scheduler.get_lr()[0], t)

            self.loss = Variable(torch.Tensor([0]))
            if t==1:
                permutations = torch.randperm(len(input_data_train))
                if model.isCuda():
                    permutations = permutations.cuda()
                    self.loss = self.loss.cuda()
            if model.isCuda():

                self.loss = self.loss.cuda()
            input_data_train = input_data_train[permutations]
            per = torch.stack([permutations*2,permutations*2+1]).transpose(1,0).contiguous().view(1,-1).squeeze()

            output_data_train = output_data_train[per]
            ena = []

            while j <= len(input_data_train):
                self.train_one_step(model, input_data_train[i:j], output_data_train[i*2:j*2, :], learning_rate, criterion,
                                    optimizer)
                i = j
                j += train_param.batch_size

                '''for group in optimizer.param_groups:
                    i = 0
                    for p in group['params']:
                        i = i+1
                        if i ==15:

                            r1 = p.data[0][0]'''

            if i < len(input_data_train):
                self.train_one_step(model, input_data_train[i:], output_data_train[i*2:, :], learning_rate, criterion,
                                    optimizer)

            if (t - 1) % train_param.log_interval == 0:

                self.loss = self.loss * train_param.batch_size / len(input_data_train)
                if t == 1:
                    oldLoss = self.loss

                print('Epoch: ', t, ' loss: ', self.loss.data[0])
                time_d = datetime.now() - starting_time
                writer.add_scalar('data/time', t, time_d.total_seconds())
                writer.add_scalar('data/training_loss', math.log(self.loss), t)
                writer.add_scalar('data/epochs_speed',
                                  60 * train_param.log_interval / (time_d.total_seconds() - old_time_d), t)
                writer.add_scalar('data/gradient_of_performance', (self.loss - oldLoss) / train_param.log_interval, t)
                old_time_d = time_d.total_seconds()
                oldLoss = self.loss

            if (t - 1) % train_param.validation_interval == 0:
                y_val = model(input_data_validate)

                val_loss = criterion(y_val, output_data_validate[:, :])

                writer.add_scalar('data/val_loss', math.log(val_loss), t)
                if val_loss < bestValLoss:
                    bestValLoss = val_loss
                    best_nn_parameters = copy.deepcopy(model.state_dict())
                    saving_epochs = t
                    torch.save(model.state_dict(), path + '/net_parameters')

                if val_loss > bestValLoss:  # oldValLoss:
                    val_count = val_count + 1

                else:

                    val_count = 0

                oldValLoss = val_loss
                writer.add_scalar('data/val_count', val_count, t)
                print('Validation: ', t, ' loss: ', val_loss, ' best loss:', bestValLoss)

                if (t - 1) % 10 == 0:
                    state = model.state_dict()
                    mean_dict = dict()
                    max_dict = dict()
                    min_dict = dict()
                    var_dict = dict()
                    for group in state:
                        mean = torch.mean(state[group])
                        max = torch.max(state[group])
                        min = torch.min(state[group])
                        var = torch.var(state[group])
                        mean_dict[group] = mean
                        max_dict[group] = max
                        min_dict[group] = min
                        var_dict[group] = var

                    writer.add_scalars('data/mean', mean_dict, t)
                    writer.add_scalars('data/max', max_dict, t)
                    writer.add_scalars('data/min', min_dict, t)
                    writer.add_scalars('data/var', var_dict, t)

                if self.plot_im:
                    fig = plt.figure()

                    # Set up sub-plotting if the model has an STN module
                    try:
                        assert(model.stn)
                        plt.subplot(121)
                    except:
                        pass

                    try:
                        plt.imshow(np.reshape(input_data_validate.data[0].cpu().numpy(), (model.image_size[0], model.image_size[1])),
                                   cmap='gray', extent=[0, model.image_size[0], model.image_size[1], 0])
                    except:
                        try:
                            plt.imshow(np.reshape(input_data_validate.data[0].cpu().numpy(), (model.image_size, model.image_size)),
                                       cmap='gray', extent=[0, model.image_size, model.image_size, 0])
                        except:
                            raise

                    plt.plot(output_data_validate.data[0].cpu().numpy(), output_data_validate.data[1].cpu().numpy(), '-b', label='actual')
                    plt.plot(y_val.data[0].cpu().numpy(), y_val.data[1].cpu().numpy(), '-r', label='predicted')
                    plt.legend()
                    try:
                        plt.xlim([0, model.image_size[0]])
                        plt.ylim([model.image_size[1], 0])
                    except:
                        try:
                            plt.xlim([0, model.image_size])
                            plt.ylim([model.image_size, 0])
                        except:
                            raise

                    # Try plotting spatial transformer network (STN) output
                    # if model contains an STN module (e.g. STIMEDNet)
                    try:
                        assert(model.stn)
                        plt.subplot(122)
                        stn_val_image, stn_val_theta = model.stn(input_data_validate[0].reshape(-1,model.image_size[2],model.image_size[0],model.image_size[1]))
                        plt.imshow(np.reshape(stn_val_image.data[0].cpu().numpy(), (model.grid_size[0], model.grid_size[1])), cmap='gray', extent=[0, model.grid_size[0], model.grid_size[1], 0])
                    except:
                        pass

                    fig.canvas.draw()
                    matrix = np.fromstring(fig.canvas.tostring_rgb(), dtype=np.uint8, sep='')

                    mat= matrix.reshape(fig.canvas.get_width_height()[::-1] + (3,))

                    writer.add_image('image' + str(t), mat)
                    self.plot_im = False

                    # torch.save(model.state_dict(), path + '/net_parameters' + str(t))

            if (t - 1) % train_param.test_interval == 0:
                y_test = model(input_data_test)
                test_loss = criterion(y_test, output_data_test[:, :])
                writer.add_scalar('data/test_loss', math.log(test_loss), t)

            '''if (t-1) % 1500 == 0:
                optimizer.reset = True
                print('reset optimizer')
            '''
            if self.resetting_optimizer:
                optimizer.reset = True

            if val_count == 7 or (t - 1) % 500==0:
                train_param.stop_criterion = "reset optimizer"
                optimizer.reset = True

            # End condition
            if inf_k * t > inf_k * train_param.epochs:
                self.train = False
                train_param.stop_criterion = "max epochs reached"

            if val_count > train_param.val_fail:
                self.train = False
                train_param.stop_criterion = "max validation fail reached"

            '''
            writer.add_scalar('data/test_lr', lr, t)

            writer.add_scalar('data/loss_lr', self.loss.data[0], lr)

            lr = 1.1**(t/40)-1
            print(lr)
            for param_group in optimizer.param_groups:
                param_group['lr'] = lr
            '''

        train_param.real_epochs = t
        train_param.min_train_loss = self.loss.data[0]
        train_param.min_val_loss = bestValLoss
        train_param.min_test_loss = test_loss.data[0]
        train_param.elapsed_time = time_d.total_seconds()
        train_param.val_count = val_count
        k = (self.loss - oldLoss) / train_param.log_interval
        train_param.min_grad = k.data[0]
        train_param.stop_criterion = train_param.stop_criterion + self.user_stop

        file.write('\n' + str(optimizer))
        file.write('\n' + str(criterion))
        file.write('\n saving_epochs = ' + str(saving_epochs))
        file.write(train_param.write_out_after())
        writer.close()

        if self._launch_tensorboard:
            print('Terminating tensorboard with process id: {}'.format(tensorboard_process.pid))
            tensorboard_process.terminate()

        print('Training finished\n')

        return best_nn_parameters

    def train_one_step(self, model, x, y, learning_rate, criterion, optimizer):
        def wrap():
            # loss=0
            optimizer.zero_grad()
            y_pred = model(x)
            # print("*************y?????????{}***********".format(len(y)))
            # print("*************y?????????{}***********".format(y.shape))
            # print("*************y_pred?????????{}***********".format(len(y_pred)))
            # print("*************y_pred?????????{}***********".format(y_pred.shape))
            # for i in range(y.shape[0]):
            #         loss += criterion(y_pred[i], y[i])
            loss=criterion(y_pred,y)
            loss.backward()
            return loss

        '''
        y_pred = model(x) # output from the network
        loss = criterion(y_pred,y) #loss
        optimizer.zero_grad()# setting gradients to zero
        loss.backward()# calculating gradients for every layer

        optimizer.step()#updating weights'''
        loss = optimizer.step(wrap)

        self.loss = self.loss + loss.item()

    def cancel_training(self):
        self.user_stop = "User stop"
        self.train = False

    def plot_image(self):
        print("plot image")
        self.plot_im = True

    def reset_ADAM(self):
        self.resetting_optimizer = True
        print('reseting ADAM')
