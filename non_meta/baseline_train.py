import datetime
import gc
import json
import logging
import os
import sys
import time
import warnings

import numpy as np
import torch
import torch.nn.functional as F
from torch import optim
from torch.nn.functional import huber_loss
from torch_geometric.loader import DataLoader, NeighborLoader
from torch.utils.tensorboard import SummaryWriter
from data.data_load import importance_graph_load, counting_graph_load
from data.dataset import PretrainDataset
from model.baselinemodel import BaseGNN

warnings.filterwarnings("ignore")
torch.autograd.set_detect_anomaly(True)
INF = float("inf")

train_config = {
    "base": 2,
    "cuda": True,
    "gpu_id": 5,
    "num_workers": 16,
    "epochs": 100,
    "batch_size": 32,
    "update_every": 4,  # actual batch_sizer = batch_size * update_every
    "print_every": 10,
    "init_emb": True,  # None, Normal
    "share_emb": False,  # sharing embedding requires the same vector length
    "share_arch": False,  # sharing architectures
    "dropout": 0.4,
    "dropatt": 0.2,
    "cv": False,
    "reg_loss": "HUBER",  # MAE, MSEl
    "bp_loss": "HUBER",  # MAE, MSE
    "bp_loss_slp": "anneal_cosine$1.0$0.01",  # 0, 0.01, logistic$1.0$0.01, linear$1.0$0.01, cosine$1.0$0.01,
    # cyclical_logistic$1.0$0.01, cyclical_linear$1.0$0.01, cyclical_cosine$1.0$0.01
    # anneal_logistic$1.0$0.01, anneal_linear$1.0$0.01, anneal_cosine$1.0$0.01
    "lr": 0.0006,
    "weight_decay": 0.0005,
    "weight_decay_var": 0.1,
    "weight_decay_film": 0.0001,
    "decay_factor": 0.1,
    "attr_ratio": 0.5,
    "decay_patience": 20,
    "max_grad_norm": 8,
    "model": "GAT",  # Graphormer
    "emb_dim": 32,
    "activation_function": "relu",  # sigmoid, softmax, tanh, relu, leaky_relu, prelu, gelu
    # MeanAttnPredictNet, SumAttnPredictNet, MaxAttnPredictNet,
    # MeanMemAttnPredictNet, SumMemAttnPredictNet, MaxMemAttnPredictNet,
    # DIAMNet, CCANet
    "mem_len": 1,
    "predict_net_mem_init": "mean",
    # mean, sum, max, attn, circular_mean, circular_sum, circular_max, circular_attn, lstm
    "predict_net_recurrent_steps": 3,
    "edgemean_num_bases": 8,
    "edgemean_graph_num_layers": 3,
    "edgemean_pattern_num_layers": 3,
    "edgemean_hidden_dim": 32,
    "init_g_dim": 11,
    "init_e_dim": 4,
    "num_g_hid": 32,
    "num_e_hid": 32,
    "out_g_ch": 32,
    "graph_num_layers": 5,
    "queryset_dir": "queryset",
    "true_card_dir": "label",
    "dataset": "web-spam",
    "data_dir": "dataset",
    "dataset_name": "web-spam",
    "save_res_dir": "result_baseline",
    "save_model_dir": "saved_model_baseline",
    "test_only": False,
}


def train(
        model,
        optimizer,
        scheduler,
        data_type,
        data_loader,
        device,
        config,
        epoch,
        logger=None,
        writer=None,
        bottleneck=False,
):
    global bp_crit, reg_crit
    epoch_step = len(data_loader)
    total_step = config["epochs"] * epoch_step
    total_var_loss = 0
    total_reg_loss = 0
    total_bp_loss = 0
    total_cnt = 1e-6
    if config["reg_loss"] == "MAE":
        reg_crit = lambda pred, target: F.l1_loss(F.relu(pred), target)
    elif config["reg_loss"] == "MSE":
        reg_crit = lambda pred, target: F.mse_loss(F.relu(pred), target)
    elif config["reg_loss"] == "SMSE":
        reg_crit = lambda pred, target: F.smooth_l1_loss(pred, target)
    elif config["reg_loss"] == "HUBER":
        reg_crit = lambda pred, target: F.huber_loss(pred, target, delta=0.1)
    if config["bp_loss"] == "MAE":
        bp_crit = lambda pred, target: F.l1_loss(F.leaky_relu(pred), target)
    elif config["bp_loss"] == "MSE":
        bp_crit = lambda pred, target: F.mse_loss(pred, target)
    elif config["bp_loss"] == "SMSE":
        bp_crit = lambda pred, target: F.smooth_l1_loss(pred, target)
    elif config["bp_loss"] == "HUBER":
        bp_crit = lambda pred, target: F.huber_loss(pred, target, delta=0.1)
    # data preparation
    # config['init_pe_dim'] = graph.edge_attr.size(1)
    if bottleneck:
        model.load_state_dict(
            torch.load(
                os.path.join(
                    save_model_dir,
                    "best_epoch_{:s}_{:s}.pt".format(
                        train_config["predict_net"], train_config["graph_net"]
                    ),
                )
            )
        )
    model.to(device)

    model.train()
    total_time = 0
    for i, batch in enumerate(data_loader):
        batch = batch.to(device)
        batch.x = batch.x.to(torch.float32)
        s = time.time()
        if config['model'] == "Graphormer":
            pred, importance_loss = model(batch)
            total_loss_per_step = importance_loss
        else:
            importance_loss = model(batch)
            total_loss_per_step = importance_loss
            total_loss_per_step.backward()
            if (i + 1) % config['update_every'] == 0:
                optimizer.step()
                optimizer.zero_grad()
        bp_loss_item = total_loss_per_step.item()
        total_bp_loss += bp_loss_item

        if writer:
            writer.add_scalar(
                "%s/BP-%s" % (data_type, config["bp_loss"]),
                bp_loss_item,
                epoch * epoch_step + i,
            )

        if logger and (
                (i / config["batch_size"]) % config["print_every"] == 0
                or i == epoch_step - 1
        ):
            logger.info(
                "epoch: {:0>3d}/{:0>3d}\tdata_type: {:<5s}\tbatch: {:0>5d}/{:0>5d}\tbp loss: {:.5f}\t".format(
                    int(epoch),
                    int(config["epochs"]),
                    data_type,
                    int(i / config["batch_size"]),
                    int(epoch_step / config["batch_size"]),
                    float(bp_loss_item),
                )
            )

        if (i + 1) % config["batch_size"] == 0 or i == epoch_step - 1:
            if config["max_grad_norm"] > 0:
                torch.nn.utils.clip_grad_norm_(
                    model.parameters(), config["max_grad_norm"]
                )
            optimizer.step()
            optimizer.zero_grad()
        e = time.time()
        total_time += e - s
        total_cnt += 1
    mean_bp_loss = total_bp_loss / total_cnt
    if writer:
        writer.add_scalar(
            "%s/BP-%s-epoch" % (data_type, config["bp_loss"]), mean_bp_loss, epoch
        )
    if logger:
        logger.info(
            "epoch: {:0>3d}/{:0>3d}\tdata_type: {:<5s}\tbp loss: {:.4f}".format(
                epoch, config["epochs"], data_type, mean_bp_loss
            )
        )

    gc.collect()
    return mean_bp_loss, total_time


def evaluate(model, data_type, data_loader, config, logger=None, writer=None):
    epoch_step = len(data_loader)
    total_step = config["epochs"] * epoch_step
    total_var_loss = 0
    total_reg_loss = 0
    total_bp_loss = 0
    total_cnt = 1e-6

    evaluate_results = {"mean": {"importance": list()},
                        "error": {"importance_loss": list()},
                        "time": {"avg": list(), "total": 0.0}}
    model.eval()
    model = model.to("cpu")
    total_time = 0
    with torch.no_grad():
        for batch_id, batch in enumerate(data_loader):
            batch.x = batch.x.to(torch.float32)
            st = time.time()
            importance = batch.degree_centrality
            evaluate_results["mean"]["importance"].extend(importance.view(-1).tolist())
            if config['model'] == "Graphormer":
                pred, importance_loss = model(batch)
                bp_loss = importance_loss
            else:
                importance_loss = model(batch)
                bp_loss = importance_loss
            et = time.time()
            evaluate_results["time"]["total"] += et - st
            avg_t = et - st

            evaluate_results["time"]["avg"].extend([avg_t])
            bp_loss_item = bp_loss.mean().item()
            total_bp_loss += bp_loss_item
            evaluate_results["error"]["importance_loss"].extend(importance_loss.view(-1).tolist())
            et = time.time()
            total_time += et - st
            total_cnt += 1
        mean_bp_loss = total_bp_loss / total_cnt
        if logger and batch_id == epoch_step - 1 and config["test_only"] is False:
            logger.info(
                "epoch: {:0>3d}/{:0>3d}\tdata_type: {:<5s}\tbatch: {:d}/{:d}\tbp loss: {:.4f}\t".format(
                    int(epoch),
                    int(config["epochs"]),
                    (data_type),
                    int(batch_id),
                    int(epoch_step),
                    float(bp_loss_item),
                )
            )
            # float(var), float(pred_var[0].item())))
            # "ground: {:.4f}\tpredict: {:.4f}"

        if logger and config["test_only"] is False:
            logger.info(
                "epoch: {:0>3d}/{:0>3d}\tdata_type: {:<5s}\t\tbp loss: {:.4f}".format(
                    epoch, config["epochs"], data_type, mean_bp_loss
                )
            )

    gc.collect()
    return mean_bp_loss, evaluate_results, total_time


def model_test(save_model_dir, test_loaders, config, logger, writer):
    total_test_time = 0
    model.load_state_dict(
        torch.load(
            os.path.join(
                save_model_dir,
                "best_epoch_{:s}.pt".format(train_config["model"]),
            )
        )
    )
    # print(model)
    mean_bp_loss, evaluate_results, _time = evaluate(
        model=model,
        data_type="test",
        data_loader=test_loaders,
        config=config,
        logger=logger,
        writer=writer,
    )
    total_test_time += _time
    # if mean_reg_loss <= best_reg_losses['test']:
    #     best_reg_losses['test'] = mean_reg_loss
    # best_reg_epochs['test'] =
    logger.info(
        "data_type: {:<5s}\tbest mean loss: {:.3f}".format("test", mean_bp_loss)
    )
    with open(
            os.path.join(
                save_model_dir,
                "%s_%s_%s_pre_trained.json"
                % (
                        train_config["model"],
                        "best_test",
                        train_config["dataset"],
                ),
            ),
            "w",
    ) as f:
        json.dump(evaluate_results, f)

    return evaluate_results, total_test_time


if __name__ == "__main__":
    torch.manual_seed(0)
    np.random.seed(0)

    for i in range(1, len(sys.argv), 2):
        arg = sys.argv[i]
        value = sys.argv[i + 1]

        if arg.startswith("--"):
            arg = arg[2:]
        if arg not in train_config:
            print("Warning: %s is not surported now." % (arg))
            continue
        train_config[arg] = value
        try:
            value = eval(value)
            if isinstance(value, (int, float)):
                train_config[arg] = value
        except:
            pass

    ts = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    model_name = "%s_%s" % (train_config["model"], ts)
    save_model_dir = train_config["save_model_dir"]
    os.makedirs(save_model_dir, exist_ok=True)

    # save config
    with open(os.path.join(save_model_dir, "train_config.json"), "w") as f:
        json.dump(train_config, f)

    # set logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        fmt="[ %(asctime)s ] %(message)s", datefmt="%a %b %d %H:%M:%S %Y"
    )
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    local_time = time.strftime("%Y-%m-%d_%H%M%S", time.localtime())
    logfile = logging.FileHandler(
        os.path.join(save_model_dir, "train_log_{:s}.txt".format(local_time)), "w"
    )
    logfile.setFormatter(fmt)
    logger.addHandler(logfile)

    # set device
    device = torch.device(
        "cuda:%d" % train_config["gpu_id"] if (train_config["gpu_id"] != -1 and train_config["cuda"] == True) else "cpu"
    )
    if train_config["gpu_id"] != -1 and train_config["cuda"] == True:
        torch.cuda.set_device(device)

        # load data
        # os.makedirs(train_config["save_data_dir"], exist_ok=True)
    # decompose the query
    if train_config['dataset'] == "flixster":
        data = PretrainDataset(name=train_config['dataset'], filepath=train_config['dataset'])[0][0]
    else:
        data = PretrainDataset(name=train_config['dataset'], filepath=train_config['dataset'])[0]
        kwargs = dict(
            data=data,
            num_neighbors=[10, 10] * 2,
            batch_size=train_config["batch_size"],
            num_workers=8,
            pin_memory=True
        )
        train_idx = data.train_mask.nonzero(as_tuple=False).view(-1)
        val_idx = data.val_mask.nonzero(as_tuple=False).view(-1)
        test_idx = data.val_mask.nonzero(as_tuple=False).view(-1)
        train_loader = NeighborLoader(
            input_nodes=train_idx,
            shuffle=True,
            **kwargs,
        )
        val_loader = NeighborLoader(
            input_nodes=val_idx,
            shuffle=False,
            **kwargs,
        )
        test_loader = NeighborLoader(
            input_nodes=test_idx,
            shuffle=False,
            **kwargs,
        )
    # config['init_g_dim'] = graph.x.size(1)
    # train_config.update({'init_g_dim': graph.x.size(1)})
    # construct the model
    # train_config["init_g_dim"] = next(iter(train_loader)).x.shape[1]
    if train_config["model"] == "GIN" or train_config["model"] == "GAT" or train_config['model'] == "GCN" or \
            train_config["model"] == "GraphSage":
        model = BaseGNN(
            train_config['model'],
            train_config["graph_num_layers"],
            train_config["init_g_dim"],
            train_config["num_g_hid"],
            train_config["out_g_ch"],
            train_config["dropout"],
        )
    else:
        raise NotImplementedError(
            "Currently, the %s model is not supported" % (train_config["model"])
        )
    # model = torch.compile(model)
    logger.info(model)
    logger.info(
        "num of parameters: %d"
        % (sum(p.numel() for p in model.parameters() if p.requires_grad))
    )

    # optimizer and losses
    writer = SummaryWriter()
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=train_config["lr"],
        weight_decay=train_config["weight_decay"],
        eps=1e-6,
    )
    optimizer.zero_grad()
    scheduler = torch.optim.lr_scheduler.ExponentialLR(
        optimizer, gamma=train_config["decay_factor"]
    )
    best_bp_losses = INF
    best_bp_epochs = {"train": -1, "val": -1, "test": -1}
    torch.backends.cudnn.benchmark = True
    total_train_time = 0
    total_dev_time = 0
    total_test_time = 0
    cur_reg_loss = {}
    if train_config["test_only"]:
        evaluate_results, total_test_time = model_test(
            save_model_dir, test_loader, train_config, logger, writer
        )
        exit(0)
    tolerance_cnt = 0
    for epoch in range(train_config["epochs"]):
        # if train_config['cv'] == True:
        #     cross_validate(model=model, query_set=QS, device=device, config=train_config, graph=graph, logger=logger,
        #                    writer=writer)
        # else:
        mean_bp_loss, _time = train(
            model,
            optimizer=optimizer,
            scheduler=scheduler,
            data_type="train",
            data_loader=train_loader,
            device=device,
            config=train_config,
            epoch=epoch,
            logger=logger,
            writer=writer,
            bottleneck=False,
        )
        total_train_time += _time
        if scheduler and (epoch + 1) % train_config["decay_patience"] == 0:
            scheduler.step()
            # torch.save(model.state_dict(), os.path.join(save_model_dir, 'epoch%d.pt' % (epoch)))
        mean_bp_loss, evaluate_results, total_time = evaluate(
            model=model,
            data_type="val",
            data_loader=val_loader,
            config=train_config,
            logger=logger,
            writer=writer,
        )
        if writer:
            writer.add_scalar(
                "%s/BP-%s-epoch" % ("val", train_config["bp_loss"]), mean_bp_loss, epoch
            )
            total_dev_time += total_time
            # cur_reg_loss[loader_idx] = mean_reg_loss
            # flag = True
            # for key1, key2 in zip(cur_reg_loss.keys(), best_reg_losses.keys()):
            #     if cur_reg_loss[key1] > best_reg_losses[key2]:
            #         flag = False
            # if flag:
            #     for key1, key2 in zip(cur_reg_loss.keys(), best_reg_losses.keys()):
            #         best_reg_losses[key2] = cur_reg_loss[key1]
            #     best_reg_epochs['val'] = epoch
        err = best_bp_losses - mean_bp_loss
        if err > 1e-4:
            tolerance_cnt = 0
            best_bp_losses = mean_bp_loss
            # best_reg_epochs["val"] = epoch
            logger.info(
                "data_type: {:<5s}\t\tbest mean loss: {:.3f} (epoch: {:0>3d})".format(
                    "val", mean_bp_loss, epoch
                )
            )
            torch.save(
                model.state_dict(),
                os.path.join(
                    save_model_dir,
                    "best_epoch_{:s}.pt".format(train_config["model"]),
                ),
            )
            with open(
                    os.path.join(save_model_dir, "%s_%d.json" % ("val", epoch)), "w"
            ) as f:
                json.dump(evaluate_results, f)
                # for data_type in data_loaders.keys():
                #     logger.info(
                #         "data_type: {:<5s}\tbest mean loss: {:.3f} (epoch: {:0>3d})".format(data_type,
                #                                                                             best_reg_losses[data_type],
                #                                                                             best_reg_epochs[data_type]))
        tolerance_cnt += 1
        if tolerance_cnt >= 20:
            break
    print("data finish")
    evaluate_results, total_test_time = model_test(
        save_model_dir, test_loader, train_config, logger, writer
    )
    logger.info(
        "train time: {:.3f}, train time per epoch :{:.3f}, test time: {:.3f}, all time: {:.3f}".format(
            total_train_time,
            total_train_time / train_config["epochs"],
            total_test_time,
            total_train_time + total_dev_time + total_test_time,
        )
    )