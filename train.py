import os
import time
import math
import argparse
from dataclasses import dataclass

import torch
from torch.utils import data
from CLIP.provider import CLIPProvider
from MLM.provider import MLMProvider
from ALBEF.provider import ALBEFProvider


@dataclass
class TrainConfig:
    data_path: tuple = ("/home/robin/Downloads/cc12m", "/home/robin/Downloads/cc3m")
    eval_ratio: float = 0.05
    batch_size: int = 64
    num_workers: int = 2
    lr: float = 1e-4
    min_lr: float = 1e-6
    grad_clip: float = 1.0
    seq_len: int = 64
    log_iters: int = 2000
    eval_iters: int = 20000
    warmup_iters: int = 2000
    lr_decay_iters: int = 512000
    max_iters: int = 1000000


ckpt_dir = "out"


class Trainer:
    def __init__(self, config, args):
        self.config = config
        self.device_type = "cuda"
        self.dtype = "bfloat16"
        enabled = self.dtype == "bfloat16"
        self.scaler = torch.cuda.amp.GradScaler(enabled=enabled)
        self.ctx = torch.amp.autocast(
            device_type=self.device_type, dtype=torch.bfloat16
        )

        if args.provider == "CLIP":
            self.train_provider = CLIPProvider(config)
        elif args.provider == "MLM":
            config.batch_size = 64
            self.train_provider = MLMProvider(config)
        elif args.provider == "ALBEF":
            config.batch_size = 48
            self.train_provider = ALBEFProvider(config)

        train_ds, eval_ds = self.train_provider.get_datasets(config)
        self.train_loader = data.DataLoader(
            train_ds,
            config.batch_size,
            num_workers=config.num_workers,
            shuffle=True,
            pin_memory=True,
        )
        self.train_batch_iter = iter(self.train_loader)

        self.val_loader = data.DataLoader(
            eval_ds,
            config.batch_size,
            num_workers=config.num_workers,
            shuffle=False,
            pin_memory=True,
        )

    def train_loop(self, model, optimizer):
        try:
            data_entry = next(self.train_batch_iter)
            if len(data_entry[0]) < self.config.batch_size:
                self.train_batch_iter = iter(self.train_loader)
                data_entry = next(self.train_batch_iter)
        except StopIteration:
            self.train_batch_iter = iter(self.train_loader)
            data_entry = next(self.train_batch_iter)

        train_result = self.train_provider.train_step(model, data_entry, self.ctx)
        loss = train_result[-1]

        self.scaler.scale(loss).backward()
        self.scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), self.config.grad_clip)
        self.scaler.step(optimizer)
        self.scaler.update()
        optimizer.zero_grad(set_to_none=True)

        return train_result

    def get_lr(self, iteration):
        config = self.config
        # 1) linear warmup for warmup_iters steps
        if iteration < config.warmup_iters:
            return config.lr * iteration / config.warmup_iters
        # 2) if it > lr_decay_iters, return min learning rate
        if iteration > config.lr_decay_iters:
            return config.min_lr
        # 3) in between, use cosine decay down to min learning rate
        decay_ratio = (iteration - config.warmup_iters) / (
            config.lr_decay_iters - config.warmup_iters
        )
        assert 0 <= decay_ratio <= 1
        coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))  # coeff ranges 0..1
        return config.min_lr + coeff * (config.lr - config.min_lr)

    @torch.no_grad()
    def validate(self, model):
        model.eval()

        total_loss = 0.0
        batch_iter = iter(self.val_loader)
        sum_accuracy = 0
        length = len(self.val_loader)
        for _ in range(length - 1):
            data_entry = next(batch_iter)
            accuracy, loss = self.train_provider.get_validate_accuracy(
                data_entry, model, self.ctx, self.device_type
            )
            sum_accuracy += accuracy
            total_loss += loss

        avg_loss = total_loss / length
        avg_accuracy = sum_accuracy / length

        model.train()
        return avg_loss, avg_accuracy

    def train(self, args):
        if args.resume:
            checkpoint = torch.load(args.resume, map_location=self.device_type)
            model = checkpoint["model"]
        else:
            model = self.train_provider.construct_model(self.config).cuda()
        cmodel = torch.compile(model)
        optimizer = torch.optim.AdamW(
            cmodel.parameters(),
            lr=self.config.lr,
            weight_decay=0.0,
            amsgrad=True,
        )
        best_val_accuracy = 1e-9
        begin = time.time()

        for iteration in range(self.config.max_iters):
            lr = self.get_lr(iteration)
            for param_group in optimizer.param_groups:
                param_group["lr"] = lr

            train_result = self.train_loop(cmodel, optimizer)

            if iteration % self.config.log_iters == 0 and iteration > 0:
                epoch, accuracy, loss = self.train_provider.get_metrics(
                    train_result, self.device_type, iteration, self.train_loader
                )
                now = time.time()
                duration = now - begin
                begin = now
                print(
                    (
                        f"[{epoch:03d} : {iteration:06d}] loss: {loss.item():.4f} "
                        f"accu: {accuracy:.4f} lr: {lr:.4e} time: {duration:.2f}"
                    )
                )
            if iteration % self.config.eval_iters == 0 and iteration > 0:
                avg_loss, avg_accuracy = self.validate(cmodel)
                if avg_accuracy > best_val_accuracy:
                    checkpoint = {
                        "model": model,
                        "eval_accuracy": avg_accuracy,
                    }
                    torch.save(
                        checkpoint, os.path.join(ckpt_dir, f"clip_{iteration}.pt")
                    )
                print(f"[Eval] loss: {avg_loss:.4f} accuracy: {avg_accuracy:.4f}")


if __name__ == "__main__":
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.enabled = True

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--resume", default="", type=str, help="Resume from a saved checkpoint"
    )
    parser.add_argument(
        "--provider",
        default="CLIP",
        type=str,
        help="Model to be trained",
        choices=["CLIP", "MLM", "ALBEF"],
    )
    args = parser.parse_args()

    config = TrainConfig()
    trainer = Trainer(config, args)
    trainer.train(args)
