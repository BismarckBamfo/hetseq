__AUTHOR__ = "Yifan Ding"
__E_MAIL__ = "dyf0125@gmail.com"
__DATE__ = "1/31/2019"

import math
import random
import collections

import torch
import numpy as np

from tqdm import tqdm

import checkpoint_utils, distributed_utils, options, progress_bar, tasks, utils
from data import iterators
from trainer import Trainer
from meters import AverageMeter, StopwatchMeter



def main(args, init_distributed=False):
    assert args.max_tokens is not None or args.max_sentences is not None, \
        'Must specify batch size either with --max-tokens or --max-sentences'

    if torch.cuda.is_available() and not args.cpu:
        torch.cuda.set_device(args.device_id)

    #  set random seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    if init_distributed:
        args.distributed_rank = distributed_utils.distributed_init(args)

    '''
    # #ignore-ckpt-now 1
    if distributed_utils.is_master(args):
        checkpoint_utils.verify_checkpoint_directory(args.save_dir)
    '''
    print(args)

    # Setup task, e.g., translation, language modeling, etc.
    task = None
    if args.task == 'bert':
        task = tasks.LanguageModelingTask.setup_task(args)
    assert task != None

    # Load valid dataset (we load training data below, based on the latest checkpoint)
    for valid_sub_split in args.valid_subset.split(','):
        task.load_dataset(valid_sub_split, combine=False, epoch=0)

    # Build model and criterion
    model = task.build_model(args)                  # ***TODO bert model ***
    # criterion = task.build_criterion(args)
    # ignore criterion now, maybe push back in the future, leave all the things with criterion commented
    # criterion = None

    print(model)
    #print('| model {}, criterion {}'.format(args.arch, criterion.__class__.__name__))
    print('| num. model params: {} (num. trained: {})'.format(
        sum(p.numel() for p in model.parameters()),
        sum(p.numel() for p in model.parameters() if p.requires_grad),
    ))

    # Build trainer
    #trainer = Trainer(args, task, model, criterion)
    trainer = Trainer(args, task, model)
    print('| training on {} GPUs'.format(args.distributed_world_size))
    print('| max tokens per GPU = {} and max sentences per GPU = {}'.format(
        args.max_tokens,
        args.max_sentences,
    ))

    # Load the latest checkpoint if one is available and restore the
    # corresponding train iterator

    # #NOT UNDERSTAND MUCH, need to go through base class, like but not limited to
    # #torch.nn.modules.module, torch.optim.optimizer, read_state_dict, parameter, serialization
    extra_state, epoch_itr = checkpoint_utils.load_checkpoint(args, trainer)

    # Train until the learning rate gets too small
    max_epoch = args.max_epoch or math.inf
    max_update = args.max_update or math.inf

    lr = trainer.get_lr()
    train_meter = StopwatchMeter()
    train_meter.start()
    # valid_subsets = args.valid_subset.split(',')

    while (
            lr > args.min_lr
            and (epoch_itr.epoch < max_epoch
            or (epoch_itr.epoch == max_epoch
                and epoch_itr._next_epoch_itr is not None))
            and trainer.get_num_updates() < max_update
    ):
        # train for one epoch
        train(args, trainer, task, epoch_itr)                   # #revise-task 6

        if not args.disable_validation and epoch_itr.epoch % args.validate_interval == 0:
            valid_losses = validate(args, trainer, task, epoch_itr, valid_subsets)                  # #revise-task 7
        else:
            valid_losses = [None]

        # only use first validation loss to update the learning rate
        lr = trainer.lr_step(epoch_itr.epoch, valid_losses[0])

        # save checkpoint
        if epoch_itr.epoch % args.save_interval == 0:
            checkpoint_utils.save_checkpoint(args, trainer, epoch_itr, valid_losses[0])

        reload_dataset = ':' in getattr(args, 'data', '')
        # sharded data: get train iterator for next epoch
        epoch_itr = trainer.get_train_iterator(epoch_itr.epoch, load_dataset=reload_dataset)

    train_meter.stop()
    print('| done training in {:.1f} seconds'.format(train_meter.sum))


def train(args, trainer, task, epoch_itr):                  # #revise-task 7
    """Train the model for one epoch."""
    # Update parameters every N batches, CORE scaling method
    update_freq = args.update_freq[epoch_itr.epoch - 1] \
        if epoch_itr.epoch <= len(args.update_freq) else args.update_freq[-1]

    # Initialize data iterator
    itr = epoch_itr.next_epoch_itr(
        fix_batches_to_gpus=args.fix_batches_to_gpus,
        shuffle=(epoch_itr.epoch >= args.curriculum),
    )

    itr = iterators.GroupedIterator(itr, update_freq)

    progress = progress_bar.build_progress_bar(
            args, itr, epoch_itr.epoch, no_progress_bar='simple',
    )

    extra_meters = collections.defaultdict(lambda: AverageMeter())
    valid_subsets = args.valid_subset.split(',')
    max_update = args.max_update or math.inf

    # #WORKING
    if distributed_utils.is_master(args):
        loop = tqdm(enumerate(progress, start=epoch_itr.iterations_in_epoch))
    else:
        loop = enumerate(progress, start=epoch_itr.iterations_in_epoch)

    for i, samples in loop:
        #print('samples', type(samples))
        #print(len(samples),len(samples[0]), len(samples[0][0]))
        #print(samples[0])
        #continue

        log_output = trainer.train_step(samples)
        if log_output is None:
            continue

        # log mid-epoch stats
        '''
        stats = get_training_stats(trainer)
        for k, v in log_output.items():
            if k in ['loss', 'nll_loss', 'ntokens', 'nsentences', 'sample_size']:
                continue  # these are already logged above
            if 'loss' in k or k == 'accuracy':
                extra_meters[k].update(v, log_output['sample_size'])
            else:
                extra_meters[k].update(v)
            stats[k] = extra_meters[k].avg
        progress.log(stats, tag='train', step=stats['num_updates'])

        # ignore the first mini-batch in words-per-second and updates-per-second calculation
        if i == 0:
            trainer.get_meter('wps').reset()
            trainer.get_meter('ups').reset()
        '''

        num_updates = trainer.get_num_updates()
        '''
        if (
                not args.disable_validation
                and args.save_interval_updates > 0
                and num_updates % args.save_interval_updates == 0
                and num_updates > 0
        ):
            valid_losses = validate(args, trainer, task, epoch_itr, valid_subsets)                  # #revise-task 9
            checkpoint_utils.save_checkpoint(args, trainer, epoch_itr, valid_losses[0])
        '''

        if num_updates >= max_update:
            break

'''
def get_training_stats(trainer):
    stats = collections.OrderedDict()
    stats['loss'] = trainer.get_meter('train_loss')     # #training loss
    if trainer.get_meter('train_nll_loss').count > 0:
        nll_loss = trainer.get_meter('train_nll_loss')
        stats['nll_loss'] = nll_loss
    else:
        nll_loss = trainer.get_meter('train_loss')      # #null loss?
    stats['ppl'] = utils.get_perplexity(nll_loss.avg)       # #perplexity 2**null_loss.avg
    stats['wps'] = trainer.get_meter('wps')     # #words per second
    stats['ups'] = trainer.get_meter('ups')     # #updates per second
    stats['wpb'] = trainer.get_meter('wpb')     # #?words per batch?
    stats['bsz'] = trainer.get_meter('bsz')     # #batch size
    stats['num_updates'] = trainer.get_num_updates()        # #number of updates
    stats['lr'] = trainer.get_lr()      # #learning rate
    stats['gnorm'] = trainer.get_meter('gnorm')     # #?normalization
    stats['clip'] = trainer.get_meter('clip')       # #gradient clip
    stats['oom'] = trainer.get_meter('oom')         # #out of memory
    if trainer.get_meter('loss_scale') is not None:
        stats['loss_scale'] = trainer.get_meter('loss_scale')
    stats['wall'] = round(trainer.get_meter('wall').elapsed_time)       # #walk time
    stats['train_wall'] = trainer.get_meter('train_wall')       # #training walk time
    return stats
'''

'''
def validate(args, trainer, task, epoch_itr, subsets):
    """Evaluate the model on the validation set(s) and return the losses."""

    if args.fixed_validation_seed is not None:
        # set fixed seed for every validation
        utils.set_torch_seed(args.fixed_validation_seed)

    valid_losses = []
    for subset in subsets:
        # Initialize data iterator
        itr = task.get_batch_iterator(
            dataset=task.dataset(subset),
            max_tokens=args.max_tokens_valid,
            max_sentences=args.max_sentences_valid,
            max_positions=utils.resolve_max_positions(
                task.max_positions(),
                trainer.get_model().max_positions(),
            ),
            ignore_invalid_inputs=args.skip_invalid_size_inputs_valid_test,
            required_batch_size_multiple=args.required_batch_size_multiple,
            seed=args.seed,
            num_shards=args.distributed_world_size,
            shard_id=args.distributed_rank,
            num_workers=args.num_workers,
        ).next_epoch_itr(shuffle=False)
        progress = progress_bar.build_progress_bar(
            args, itr, epoch_itr.epoch,
            prefix='valid on \'{}\' subset'.format(subset),
            no_progress_bar='simple'
        )

        # reset validation loss meters
        for k in ['valid_loss', 'valid_nll_loss']:
            meter = trainer.get_meter(k)
            if meter is not None:
                meter.reset()
        extra_meters = collections.defaultdict(lambda: AverageMeter())

        for sample in progress:
            log_output = trainer.valid_step(sample)

            for k, v in log_output.items():
                if k in ['loss', 'nll_loss', 'ntokens', 'nsentences', 'sample_size']:
                    continue
                extra_meters[k].update(v)

        # log validation stats
        stats = get_valid_stats(trainer, args, extra_meters)
        for k, meter in extra_meters.items():
            stats[k] = meter.avg
        progress.print(stats, tag=subset, step=trainer.get_num_updates())

        valid_losses.append(
            stats[args.best_checkpoint_metric].avg
            if args.best_checkpoint_metric == 'loss'
            else stats[args.best_checkpoint_metric]
        )
    return valid_losses
'''

'''
def get_valid_stats(trainer, args, extra_meters=None):
    stats = collections.OrderedDict()
    stats['loss'] = trainer.get_meter('valid_loss')
    if trainer.get_meter('valid_nll_loss').count > 0:
        nll_loss = trainer.get_meter('valid_nll_loss')
        stats['nll_loss'] = nll_loss
    else:
        nll_loss = stats['loss']
    stats['ppl'] = utils.get_perplexity(nll_loss.avg)
    stats['num_updates'] = trainer.get_num_updates()
    if hasattr(checkpoint_utils.save_checkpoint, 'best'):
        key = 'best_{0}'.format(args.best_checkpoint_metric)
        best_function = max if args.maximize_best_checkpoint_metric else min

        current_metric = None
        if args.best_checkpoint_metric == 'loss':
            current_metric = stats['loss'].avg
        elif args.best_checkpoint_metric in extra_meters:
            current_metric = extra_meters[args.best_checkpoint_metric].avg
        elif args.best_checkpoint_metric in stats:
            current_metric = stats[args.best_checkpoint_metric]
        else:
            raise ValueError("best_checkpoint_metric not found in logs")

        stats[key] = best_function(
            checkpoint_utils.save_checkpoint.best,
            current_metric,
        )
    return stats
'''


def distributed_main(i, args, start_rank=0):
    args.device_id = i
    if args.distributed_rank is None:  # torch.multiprocessing.spawn
        args.distributed_rank = start_rank + i
    main(args, init_distributed=True)


def cli_main(task):
    parser = options.get_training_parser(task)
    args = options.parse_args_and_arch(parser)

    if args.distributed_init_method is not None:
        assert args.distributed_gpus <= torch.cuda.device_count()

        if args.distributed_gpus > 1 and not args.distributed_no_spawn:     # #by default run this logic
            start_rank = args.distributed_rank
            args.distributed_rank = None  # assign automatically
            torch.multiprocessing.spawn(
                fn=distributed_main,
                args=(args, start_rank),
                nprocs=args.distributed_gpus,
            )
        else:
            distributed_main(args.device_id, args)
    elif args.distributed_world_size > 1:
        # fallback for single node with multiple GPUs
        assert args.distributed_world_size <= torch.cuda.device_count()
        port = random.randint(10000, 20000)
        args.distributed_init_method = 'tcp://localhost:{port}'.format(port=port)
        args.distributed_rank = None  # set based on device id
        if max(args.update_freq) > 1 and args.ddp_backend != 'no_c10d':
            print('| NOTE: you may get better performance with: --ddp-backend=no_c10d')
        torch.multiprocessing.spawn(
            fn=distributed_main,
            args=(args, ),
            nprocs=args.distributed_world_size,
        )
    else:
        # single GPU training
        main(args)


if __name__ == "__main__":
    task = 'bert'
    cli_main(task)