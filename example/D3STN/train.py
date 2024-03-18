import os
import random
import time

import numpy as np
import paddle
import paddle.io as io
import paddle.nn as nn
import paddle.optimizer as optim
import sklearn.metrics as metrics
import visualdl
from args import args
from d3stn import D3STN
from dataset import TrafficFlowDataset
from utils import (
    CosineAnnealingWithWarmupDecay,
    Logger,
    get_adjacency_matrix_2direction,
    masked_mape_np,
    norm_adj_matrix,
)

from paddlexde.solver.fixed_solver import RK4, Euler, Midpoint


class Trainer:
    def __init__(self, training_args):

        self.training_args = training_args

        # 创建文件保存位置
        self.folder_dir = (
            f"MAE_{training_args.model_name}_elayer{training_args.encoder_num_layers}_"
            + f"dlayer{training_args.decoder_num_layers}_head{training_args.head}_dm{training_args.d_model}_"
            + f"einput{training_args.encoder_input_size}_dinput{training_args.decoder_input_size}_"
            + f"doutput{training_args.decoder_output_size}_drop{training_args.dropout}_"
            + f"lr{training_args.learning_rate}_wd{training_args.weight_decay}_bs{training_args.batch_size}_"
            + f"topk{training_args.top_k}_att{training_args.attention}_trepoch{training_args.train_epochs}_"
            + f"finepoch{training_args.finetune_epochs}_dde"
        )

        self.save_path = os.path.join(
            "experiments", training_args.dataset_name, self.folder_dir
        )
        os.makedirs(self.save_path, exist_ok=True)
        self.logger = Logger("D3STN", os.path.join(self.save_path, "log.txt"))
        self.writer = visualdl.LogWriter(
            logdir=os.path.join(self.save_path, "visualdl")
        )

        # 输出文件保存信息
        if training_args.start_epoch == 0:
            self.logger.info(f"create params directory {self.save_path}")
        elif training_args.start_epoch > 0:
            self.logger.info(f"train from params directory {self.save_path}")

        self.logger.info(f"save folder: {self.folder_dir}")
        self.logger.info(f"save path  : {self.save_path}")
        self.logger.info(f"log  file  : {self.logger.log_file}")

        # 输出当前的args
        args_message = "\n".join(
            [f"{k:<20}: {v}" for k, v in vars(training_args).items()]
        )
        self.logger.info(f"training_args  : \n{args_message}")

        # 输出当前的state
        state = paddle.get_rng_state()
        state_cuda = paddle.get_cuda_rng_state()
        state_random = random.getstate()
        state_np = np.random.get_state()
        self.logger.info(f"state: {state[0].current_seed()}")
        self.logger.info(
            f"state_cuda: {state_cuda[0].current_seed() if len(state_cuda) > 0 else None}"
        )
        self.logger.info(f"state_random: {state_random[1][0]}")
        self.logger.info(f"state_np: {state_np[1][0]}")

        self._build_data()  # 创建训练数据
        self._build_model()  # 创建模型
        self._build_optim()  # 创建优化器

    def _build_data(self):
        self.train_dataset = TrafficFlowDataset(self.training_args, "train")
        self.val_dataset = TrafficFlowDataset(self.training_args, "val")
        self.test_dataset = TrafficFlowDataset(self.training_args, "test")

        self.train_dataloader = io.DataLoader(
            self.train_dataset,
            batch_size=self.training_args.batch_size,
            shuffle=True,
            drop_last=True,
        )
        self.eval_dataloader = io.DataLoader(
            self.val_dataset,
            batch_size=self.training_args.batch_size,
            shuffle=False,
            drop_last=False,
        )
        self.test_dataloader = io.DataLoader(
            self.test_dataset,
            batch_size=self.training_args.batch_size,
            shuffle=False,
            drop_last=False,
        )

        # 初始化输入序列长度为12
        self.fix_week = paddle.arange(
            start=self.training_args.his_len - 2016,
            end=self.training_args.his_len - 2016 + 12,
        )
        self.fix_day = paddle.arange(
            start=self.training_args.his_len - 288,
            end=self.training_args.his_len - 288 + 12,
        )
        self.fix_hour = paddle.arange(
            start=self.training_args.his_len - 12,
            end=self.training_args.his_len,
        )
        self.fix_pred = paddle.arange(
            start=self.training_args.his_len,
            end=self.training_args.his_len + 12,
        )
        self.fix_pred = paddle.ones(shape=[self.training_args.tgt_len]) * (
            self.training_args.his_len - 1
        )

        encoder_idx = []
        decoder_idx = [self.fix_pred]

        if self.training_args.his_len >= 2016:
            # for week
            encoder_idx.append(self.fix_week)
        elif self.training_args.his_len >= 288:
            # for day
            encoder_idx.append(self.fix_day)
        elif self.training_args.his_len >= 12:
            # for hour
            encoder_idx.append(self.fix_hour)

        # concat all
        encoder_idx = paddle.concat(encoder_idx)
        decoder_idx = paddle.concat(decoder_idx)

        # 将encoder_idx和decoder_idx作为可训练参数
        self.encoder_idx = paddle.create_parameter(
            shape=encoder_idx.shape, dtype="float32"
        )
        self.decoder_idx = paddle.create_parameter(
            shape=decoder_idx.shape, dtype="float32"
        )
        self.encoder_idx.set_value(paddle.cast(encoder_idx, "float32"))
        self.decoder_idx.set_value(paddle.cast(decoder_idx, "float32"))

        self.logger.info(f"encoder_idx: {self.encoder_idx}")
        self.logger.info(f"decoder_idx: {self.decoder_idx}")

    def _build_model(self):
        default_dtype = paddle.get_default_dtype()
        # 加载邻接矩阵
        adj_matrix, _ = get_adjacency_matrix_2direction(
            self.training_args.adj_path, self.training_args.num_nodes
        )
        adj_matrix = paddle.to_tensor(norm_adj_matrix(adj_matrix), default_dtype)
        # 加载互相关矩阵
        sc_matrix = np.load(self.training_args.sc_path)[0, :, :]
        sc_matrix = paddle.to_tensor(norm_adj_matrix(sc_matrix), default_dtype)

        # 设置模型默认初始化模式
        nn.initializer.set_global_initializer(
            nn.initializer.XavierUniform(), nn.initializer.Constant(value=0.0)
        )

        self.net = D3STN(
            self.training_args,
            adj_matrix=adj_matrix,
            sc_matrix=sc_matrix,
        )

        if self.training_args.continue_training:
            self.load()

        # 输出模型信息
        self.logger.debug(self.net)

        total_param = 0
        self.logger.debug("Net's state_dict:")
        for param_tensor in self.net.state_dict():
            self.logger.debug(
                f"{param_tensor} \t {self.net.state_dict()[param_tensor].shape}"
            )
            total_param += np.prod(self.net.state_dict()[param_tensor].shape)
        self.logger.debug(f"Net's total params: {total_param}.")

    def _build_optim(self):
        self.criterion = nn.L1Loss()  # 定义损失函数

        # 创建学习率 warmup模式
        self.lr_scheduler = CosineAnnealingWithWarmupDecay(
            max_lr=1,
            min_lr=0.1,
            warmup_step=0.2 * self.training_args.train_epochs,
            decay_step=0.8 * self.training_args.train_epochs,
        )
        # 为不同的参数设置不同的训练参数
        parameters = [
            {
                "params": self.net.parameters(),
                "learning_rate": self.training_args.learning_rate,
            },
            {
                "params": [self.decoder_idx],
                "learning_rate": self.training_args.learning_rate * 0.1,
            },
            {
                "params": [self.encoder_idx],
                "learning_rate": self.training_args.learning_rate * 0.1,
            },
        ]

        # 定义优化器，传入所有网络参数
        self.optimizer = optim.Adam(
            parameters=parameters,
            learning_rate=self.lr_scheduler,
            weight_decay=self.training_args.weight_decay,
            multi_precision=True,
        )

        # 输出模型优化器信息
        self.logger.info("Optimizer's state_dict:")
        for var_name in self.optimizer.state_dict():
            self.logger.info(f"{var_name} \t {self.optimizer.state_dict()[var_name]}")

        # 输出当前微分方程使用的优化器函数
        if self.training_args.solver == "euler":
            self.dde_solver = Euler
        elif self.training_args.solver == "midpoint":
            self.dde_solver = Midpoint
        elif self.training_args.solver == "rk4":
            self.dde_solver = RK4

        self.logger.info(f"dde_solver: {self.dde_solver}")

    def save(self, epoch=None):
        if epoch is not None:
            params_filename = os.path.join(self.save_path, f"epoch_{epoch}.params")
            encoder_idx_filename = os.path.join(self.save_path, f"epoch_{epoch}.enidx")
            decoder_idx_filename = os.path.join(self.save_path, f"epoch_{epoch}.deidx")
            paddle.save(self.net.state_dict(), params_filename)
            paddle.save(self.encoder_idx, encoder_idx_filename)
            paddle.save(self.decoder_idx, decoder_idx_filename)
            self.logger.info(f"save parameters to file: {params_filename}")

        params_filename = os.path.join(self.save_path, "epoch_best.params")
        encoder_idx_filename = os.path.join(self.save_path, "epoch_best.enidx")
        decoder_idx_filename = os.path.join(self.save_path, "epoch_best.deidx")
        paddle.save(self.net.state_dict(), params_filename)
        paddle.save(self.encoder_idx, encoder_idx_filename)
        paddle.save(self.decoder_idx, decoder_idx_filename)
        self.logger.info(f"save parameters to file: {params_filename}")

    def load(self, epoch=None):
        if epoch is not None:
            params_filename = os.path.join(self.save_path, f"epoch_{epoch}.params")
            encoder_idx_filename = os.path.join(self.save_path, f"epoch_{epoch}.enidx")
            decoder_idx_filename = os.path.join(self.save_path, f"epoch_{epoch}.deidx")
        else:
            params_filename = os.path.join(self.save_path, "epoch_best.params")
            encoder_idx_filename = os.path.join(self.save_path, "epoch_best.enidx")
            decoder_idx_filename = os.path.join(self.save_path, "epoch_best.deidx")

        self.net.set_state_dict(paddle.load(params_filename))
        self.encoder_idx.set_value(paddle.load(encoder_idx_filename))
        self.decoder_idx.set_value(paddle.load(decoder_idx_filename))
        self.logger.info(f"load weight from: {params_filename}")

    def train(self):
        self.logger.info("start train...")

        s_time = time.time()
        best_eval_loss, best_epoch, global_step = np.inf, 0, 0
        for epoch in range(
            self.training_args.start_epoch, self.training_args.train_epochs
        ):
            tr_s_time = time.time()
            epoch_step = 0
            self.lr_scheduler.step()
            for batch_index, batch_data in enumerate(self.train_dataloader):
                _, training_loss = self.train_one_step(batch_data)
                self.writer.add_scalar("train/loss", training_loss, global_step)
                self.writer.add_scalar("train/lr", self.optimizer.get_lr(), global_step)
                epoch_step += 1
                global_step += 1
                # self.logger.info(f"train: {global_step} loss: {training_loss.item()}")
            self.logger.info(f"learning_rate: {self.optimizer.get_lr()}")
            self.logger.info(
                f"epoch: {epoch}, train time cost:{time.time() - tr_s_time}"
            )
            self.logger.info(f"epoch: {epoch}, total time cost:{time.time() - s_time}")

            # apply model on the validation data set
            eval_loss = self.compute_eval_loss(epoch)
            if eval_loss < best_eval_loss:
                best_eval_loss = eval_loss
                best_epoch = epoch
                self.logger.info(f"best_epoch: {best_epoch}")
                self.logger.info(f"eval_loss: {float(eval_loss)}")
                self.compute_test_loss(epoch)
                self.save(epoch=epoch)

        self.logger.info(f"best epoch: {best_epoch}")
        self.logger.info("apply the best val model on the test dataset ...")

    def finetune(self):
        self.logger.info("Start FineTune Training")
        self.load()

        self.lr_scheduler = CosineAnnealingWithWarmupDecay(
            max_lr=1,
            min_lr=0.1,
            warmup_step=0.2 * self.training_args.finetune_epochs,
            decay_step=0.8 * self.training_args.finetune_epochs,
        )

        parameters = [
            {
                "params": self.net.parameters(),
                "learning_rate": self.training_args.learning_rate * 0.1,
            },
            {
                "params": [self.decoder_idx],
                "learning_rate": self.training_args.learning_rate,
            },
            {
                "params": [self.encoder_idx],
                "learning_rate": self.training_args.learning_rate,
            },
        ]

        # 定义优化器，传入所有网络参数
        self.optimizer = optim.Adam(
            parameters=parameters,
            learning_rate=self.lr_scheduler,
            weight_decay=self.training_args.weight_decay,
            multi_precision=True,
        )

        s_time = time.time()
        best_eval_loss, best_epoch, global_step = np.inf, 0, 0
        for epoch in range(
            self.training_args.train_epochs,
            self.training_args.train_epochs + self.training_args.finetune_epochs,
        ):
            tr_s_time = time.time()
            epoch_step = 0
            self.lr_scheduler.step()
            for batch_index, batch_data in enumerate(self.train_dataloader):
                _, training_loss = self.train_one_step(batch_data)
                self.writer.add_scalar("train/loss", training_loss, global_step)
                self.writer.add_scalar("train/lr", self.optimizer.get_lr(), global_step)
                epoch_step += 1
                global_step += 1
                # self.logger.info(f"train: {global_step} loss: {training_loss.item()}")
            self.logger.info(f"learning_rate: {self.optimizer.get_lr()}")
            self.logger.info(
                f"epoch: {epoch}, train time cost:{time.time() - tr_s_time}"
            )
            self.logger.info(f"epoch: {epoch}, total time cost:{time.time() - s_time}")

            # apply model on the validation data set
            eval_loss = self.compute_eval_loss(epoch)
            if eval_loss < best_eval_loss:
                best_eval_loss = eval_loss
                best_epoch = epoch
                self.logger.info(f"best_epoch: {best_epoch}")
                self.logger.info(f"eval_loss: {float(eval_loss)}")
                self.compute_test_loss(epoch)
                self.save(epoch=epoch)

        self.logger.info(f"best epoch: {best_epoch}")
        self.logger.info("apply the best val model on the test dataset ...")

    def train_one_step(self, batch_data):
        self.net.train()
        src, src_d_idx, src_m_idx, tgt, tgt_d_idx, tgt_m_idx = batch_data
        encoder_input = src
        decoder_input = paddle.zeros_like(tgt)
        kwargs = {
            "src_d_idx": src_d_idx,
            "src_m_idx": src_m_idx,
            "tgt_d_idx": tgt_d_idx,
            "tgt_m_idx": tgt_m_idx,
        }
        preds = self.net(encoder_input, self.encoder_idx, decoder_input, **kwargs)
        loss = self.criterion(preds, tgt)

        loss.backward()
        self.optimizer.step()
        self.optimizer.clear_grad()

        return preds, loss

    def eval_one_step(self, batch_data):
        self.net.eval()
        src, src_d_idx, src_m_idx, tgt, tgt_d_idx, tgt_m_idx = batch_data
        encoder_input = src
        decoder_input = paddle.zeros_like(tgt)
        kwargs = {
            "src_d_idx": src_d_idx,
            "src_m_idx": src_m_idx,
            "tgt_d_idx": tgt_d_idx,
            "tgt_m_idx": tgt_m_idx,
        }
        preds = self.net(encoder_input, self.encoder_idx, decoder_input, **kwargs)
        loss = self.criterion(preds, tgt)

        return preds, loss

    def test_one_step(self, batch_data):
        self.net.eval()
        src, src_d_idx, src_m_idx, tgt, tgt_d_idx, tgt_m_idx = batch_data
        encoder_input = src
        decoder_input = paddle.zeros_like(tgt)
        kwargs = {
            "src_d_idx": src_d_idx,
            "src_m_idx": src_m_idx,
            "tgt_d_idx": tgt_d_idx,
            "tgt_m_idx": tgt_m_idx,
        }
        preds = self.net(encoder_input, self.encoder_idx, decoder_input, **kwargs)
        loss = self.criterion(preds, tgt)

        return preds, loss

    def compute_eval_loss(self, epoch=-1):
        with paddle.no_grad():
            all_eval_loss = paddle.zeros([1], dtype=paddle.get_default_dtype())
            start_time = time.time()
            for batch_index, batch_data in enumerate(self.eval_dataloader):
                predict_output, eval_loss = self.eval_one_step(batch_data)
                self.writer.add_scalar(f"eval/loss-{epoch}", eval_loss, batch_index)
                all_eval_loss += eval_loss

            eval_loss = all_eval_loss / len(self.eval_dataloader)
            self.logger.info(f"eval cost time: {time.time() - start_time} s")
            self.logger.info(f"eval_loss: {float(eval_loss)}")
        return eval_loss

    def compute_test_loss(self, epoch=-1):
        with paddle.no_grad():
            preds, tgts = [], []
            start_time = time.time()
            all_test_loss = paddle.zeros([1], dtype=paddle.get_default_dtype())
            for batch_index, batch_data in enumerate(self.test_dataloader):
                predict_output, test_loss = self.test_one_step(batch_data)
                self.writer.add_scalar(f"test/loss-{epoch}", test_loss, batch_index)
                all_test_loss += test_loss
                preds.append(predict_output)
                tgts.append(batch_data[3])
            test_loss = all_test_loss / len(self.test_dataloader)
            self.logger.info(f"test time on whole data: {time.time() - start_time} s")
            self.logger.info(f"test_loss: {float(test_loss)}")

            preds = paddle.concat(preds, axis=0)  # [B,N,T,1]
            trues = paddle.concat(tgts, axis=0)  # [B,N,T,F]
            # [B,N,T,1]
            preds = self.test_dataset.inverse_transform(preds, axis=-1).numpy()
            # [B,N,T,1]
            trues = self.test_dataset.inverse_transform(trues, axis=-1).numpy()

            self.logger.info(f"preds: {preds.shape}")
            self.logger.info(f"tgts: {trues.shape}")

            # 计算误差
            excel_list = []
            prediction_length = trues.shape[2]

            for i in range(prediction_length):
                assert preds.shape[0] == trues.shape[0]
                mae = metrics.mean_absolute_error(trues[:, :, i, 0], preds[:, :, i, 0])
                rmse = (
                    metrics.mean_squared_error(
                        trues[:, :, i, 0],
                        preds[:, :, i, 0],
                    )
                    ** 0.5
                )
                mape = masked_mape_np(trues[:, :, i, 0], preds[:, :, i, 0], 0)
                self.logger.info(f"{i} MAE: {mae}")
                self.logger.info(f"{i} RMSE: {rmse}")
                self.logger.info(f"{i} MAPE: {mape}")
                excel_list.extend([mae, rmse, mape])

            # print overall results
            mae = metrics.mean_absolute_error(
                trues.reshape(-1, 1), preds.reshape(-1, 1)
            )
            rmse = (
                metrics.mean_squared_error(
                    trues.reshape(-1, 1),
                    preds.reshape(-1, 1),
                )
                ** 0.5
            )
            mape = masked_mape_np(trues.reshape(-1, 1), preds.reshape(-1, 1), 0)
            self.logger.info(f"all MAE: {mae}")
            self.logger.info(f"all RMSE: {rmse}")
            self.logger.info(f"all MAPE: {mape}")
            excel_list.extend([mae, rmse, mape])
            self.logger.info(excel_list)

    def run_test(self):
        self.load()
        self.compute_test_loss()


if __name__ == "__main__":
    trainer = Trainer(training_args=args)
    trainer.train()
    trainer.finetune()
    trainer.run_test()
