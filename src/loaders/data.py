from functools import partial
import os
import gc
import torch
import logging
import torchtext
import torchvision
import transformers
import concurrent.futures

from src import TqdmToLogger, stratified_split
from src.datasets import *
from src.loaders.split import simulate_split

from transformers import BertTokenizer


logger = logging.getLogger(__name__)

MEANS = {
    'CIFAR100': [0.5071, 0.4865, 0.4409],
}

STDS = {
    'CIFAR100': [0.2673, 0.2564, 0.2762],
}

VOCABS = {
    'Flickr30k': 'vocab.txt',
    'MedicalAbstracts': 'vocab.txt'
}

    

class SubsetWrapper(torch.utils.data.Dataset):
    """Wrapper of `torch.utils.data.Subset` module for applying individual transform.
    """
    def __init__(self, subset, suffix):
        self.subset = subset
        self.suffix = suffix

    def __getitem__(self, index):
        batch = self.subset[index]
        return batch

    def __len__(self):
        return len(self.subset)
    
    def __repr__(self):
        return f'{repr(self.subset.dataset.dataset)} {self.suffix}'

def load_dataset(args, server=False):
    """Fetch and split requested datasets.
    
    Args:
        args: arguments
        
    Returns:
        split_map: {client ID: [assigned sample indices]}
            ex) {0: [indices_1], 1: [indices_2], ... , K: [indices_K]}
        server_testset: (optional) holdout dataset located at the central server, 
        client datasets: [(local training set, local test set)]
            ex) [tuple(local_training_set[indices_1], local_test_set[indices_1]), tuple(local_training_set[indices_2], local_test_set[indices_2]), ...]

    """
    TOKENIZER_STRINGS = {
        'DistilBert': 'distilbert-base-uncased',
        'SqueezeBert': 'squeezebert/squeezebert-uncased',
        'MobileBert': 'google/mobilebert-uncased'
    } 
    
    # error manager
    def _check_and_raise_error(entered, targeted, msg, eq=True):
        if eq:
            if entered == targeted: # raise error if eq(==) condition meets
                err = f'[{args.dataset.upper()}] `{entered}` {msg} is not supported for this dataset!'
                logger.exception(err)
                raise AssertionError(err)
        else:
            if entered != targeted: # raise error if neq(!=) condition meets
                err = f'[{args.dataset.upper()}] `{targeted}` {msg} is only supported for this dataset!'
                logger.exception(err)
                raise AssertionError(err)

    # method to get transformation chain
    def _get_transform(args, train=False, target=False, n_channels=3, to_pil_first=False, dataset=None):

        # NOTE: target tranform may be different from input transform, disable for both now
        if n_channels == 3:
            transform = torchvision.transforms.Compose(
                [
                    torchvision.transforms.ToPILImage() if to_pil_first else torchvision.transforms.Lambda(lambda x: x),
                    torchvision.transforms.Resize((args.resize, args.resize)) if args.resize is not None\
                        else torchvision.transforms.Lambda(lambda x: x),
                    torchvision.transforms.RandomCrop(args.crop, pad_if_needed=True, padding=4) if (args.crop is not None and train)\
                        else torchvision.transforms.CenterCrop(args.crop) if (args.crop is not None and not train)\
                            else torchvision.transforms.Lambda(lambda x: x),
                    torchvision.transforms.RandomRotation(args.randrot) if (args.randrot is not None and train)\
                        else torchvision.transforms.Lambda(lambda x: x),
                    torchvision.transforms.RandomHorizontalFlip(args.randhf) if (args.randhf is not None and train)\
                        else torchvision.transforms.Lambda(lambda x: x),
                    torchvision.transforms.RandomVerticalFlip(args.randvf) if (args.randvf is not None and train)\
                        else torchvision.transforms.Lambda(lambda x: x),
                    torchvision.transforms.ColorJitter(brightness=args.randjit, contrast=args.randjit) if (args.randjit is not None and train)\
                        else torchvision.transforms.Lambda(lambda x: x),
                    torchvision.transforms.ToTensor(),
                    torchvision.transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]) if args.imnorm and dataset is None and not target\
                        else torchvision.transforms.Normalize(mean=MEANS[dataset], std=STDS[dataset]) if args.imnorm and dataset is not None and not target\
                            else torchvision.transforms.Lambda(lambda x: x)
                ]
            )
        elif n_channels == 1:
            transform = torchvision.transforms.Compose(
                [
                    torchvision.transforms.ToPILImage() if to_pil_first else torchvision.transforms.Lambda(lambda x: x),
                    torchvision.transforms.Resize((args.resize, args.resize)) if args.resize is not None\
                        else torchvision.transforms.Lambda(lambda x: x),
                    # torchvision.transforms.RandomCrop(args.crop, pad_if_needed=True) if (args.crop is not None and train)\
                    #     else torchvision.transforms.CenterCrop(args.crop) if (args.crop is not None and not train)\
                    #         else torchvision.transforms.Lambda(lambda x: x),
                    # torchvision.transforms.RandomRotation(args.randrot) if (args.randrot is not None and train)\
                    #     else torchvision.transforms.Lambda(lambda x: x),
                    # torchvision.transforms.RandomHorizontalFlip(args.randhf) if (args.randhf is not None and train)\
                    #     else torchvision.transforms.Lambda(lambda x: x),
                    # torchvision.transforms.RandomVerticalFlip(args.randvf) if (args.randvf is not None and train)\
                    #     else torchvision.transforms.Lambda(lambda x: x),
                    # torchvision.transforms.ColorJitter(brightness=args.randjit, contrast=args.randjit) if (args.randjit is not None and train)\
                    #     else torchvision.transforms.Lambda(lambda x: x),
                    torchvision.transforms.ToTensor(),
                    torchvision.transforms.Normalize(mean=[0.5], std=[0.5]) if args.imnorm and not target\
                        else torchvision.transforms.Lambda(lambda x: x)
                ]
            )
        return transform
    
    # method to construct per-client dataset
    def _construct_dataset(raw_train, idx, sample_indices):
        task = raw_train.task if hasattr(raw_train, 'task') else None
        modality = raw_train.modality if hasattr(raw_train, 'modality') else None
        name = raw_train.name if hasattr(raw_train, 'name') else None
        subset = torch.utils.data.Subset(raw_train, sample_indices)

        if args.test_size == -1:
            training_set = subset
        else:
            if args.num_classes is None: # regression
                training_set, test_set = torch.utils.data.random_split(subset, [len(subset) - int(len(subset) * args.test_size), int(len(subset) * args.test_size)])
            else: # classification
                training_set, test_set = stratified_split(subset, args.test_size)
                
        traininig_set = SubsetWrapper(training_set, f'< {str(idx).zfill(8)} > (train)')
        if len(subset) * args.test_size > 0:
            test_set = SubsetWrapper(test_set, f'< {str(idx).zfill(8)} > (test)')
        else:
            test_set = None
        return (traininig_set, test_set, task, modality, name)
    
    #################
    # base settings #
    #################
    # required intermediate outputs
    raw_train, raw_test = None, None

    # required outputs
    split_map, client_datasets = None, None
    
    # optional argument for data transforms
    transforms = [None, None]
    
    ####################
    # for text dataset #
    ####################
    tokenizer = None
    if args.use_model_tokenizer or args.use_pt_model:
        assert args.model_name in ['DistilBert', 'SqueezeBert', 'MobileBert'], 'Please specify a proper model!'

    if args.use_model_tokenizer:
        assert args.model_name.lower() in transformers.models.__dict__.keys(), f'Please check if the model (`{args.model_name}`) is supported by `transformers` module!'
        module = transformers.models.__dict__[f'{args.model_name.lower()}']
        tokenizer = getattr(module, f'{args.model_name}Tokenizer').from_pretrained(TOKENIZER_STRINGS[args.model_name])

    if args.use_bert_tokenizer:
        if args.dataset in VOCABS.keys():
            tokenizer = BertTokenizer(os.path.join(args.data_path, VOCABS[args.dataset]))
        else:
            tokenizer = BertTokenizer.from_pretrained(
            'bert-base-uncased', do_lower_case="uncased" in 'bert_base_uncased'
        )
    #################
    # fetch dataset #
    #################
    logger.info(f'[LOAD] Fetch dataset!')
    
    if args.dataset in ['FEMNIST', 'Shakespeare', 'Sent140', 'CelebA', 'Reddit']: # 1) for a special dataset - LEAF benchmark...
        _check_and_raise_error(args.split_type, 'pre', 'split scenario', False)
        _check_and_raise_error(args.eval_type, 'local', 'evaluation type', False)
         
        # define transform
        if args.dataset in ['FEMNIST', 'CelebA']:
            # check if `crop` is required
            if args.crop is None:
                logger.info(f'[LOAD] Dataset `{args.dataset}` may require `crop` argument; (recommended: `FEMNIST` - 28, `CelebA` - 84)!')
            transforms = [_get_transform(args, train=True), _get_transform(args, train=False)]
        elif args.dataset == 'Reddit':
            args.rawsmpl = 1.0

        # construct split hashmap, client datasets
        # NOTE: for LEAF benchmark, values of `split_map` hashmap is not indices, but sample counts of tuple (training set, test set)!
        split_map, client_datasets, args = fetch_leaf(
            args=args,
            dataset_name=args.dataset, 
            root=args.data_path, 
            seed=args.seed, 
            raw_data_fraction=args.rawsmpl, 
            test_size=args.test_size, 
            transforms=transforms
        )

        # no global holdout set for LEAF
        raw_test = None  
    elif args.dataset == 'Flickr30k':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        transforms = [_get_transform(args, train=True), _get_transform(args, train=False)]
        raw_train, raw_test, args = fetch_flickr30k(args=args, root=args.data_path, transforms=transforms, tokenizer=tokenizer)
    
    elif args.dataset == 'Coco':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        transforms = [_get_transform(args, train=True), _get_transform(args, train=False)]
        raw_train, raw_test, args = fetch_coco(args=args, root=args.data_path, transforms=transforms, tokenizer=tokenizer)
    

    elif args.dataset in torchvision.datasets.__dict__.keys(): # 3) for downloadable datasets in `torchvision.datasets`...
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        transforms = [_get_transform(args, train=True, dataset=args.dataset), _get_transform(args, train=False, dataset=args.dataset)]
        raw_train, raw_test, args = fetch_torchvision_dataset(args=args, dataset_name=args.dataset, root=args.data_path, transforms=transforms)
        
    elif args.dataset in torchtext.datasets.__dict__.keys(): # 4) for downloadable datasets in `torchtext.datasets`...
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        raw_train, raw_test, args = fetch_torchtext_dataset(args=args, dataset_name=args.dataset, root=args.data_path, seq_len=args.seq_len, tokenizer=tokenizer, num_embeddings=args.num_embeddings) 
        
    elif args.dataset == 'TinyImageNet': # 5) for other public datasets...
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        transforms = [_get_transform(args, train=True), _get_transform(args, train=False)]
        raw_train, raw_test, args = fetch_tinyimagenet(args=args, root=args.data_path, transforms=transforms)
        
    elif args.dataset == 'CINIC10':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        transforms = [_get_transform(args, train=True), _get_transform(args, train=False)]
        raw_train, raw_test, args = fetch_cinic10(args=args, root=args.data_path, transforms=transforms)
    
    elif args.dataset == 'SpeechCommands':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        raw_train, raw_test, args = fetch_speechcommands(args=args, root=args.data_path)

    elif 'BeerReviews' in args.dataset:
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        aspect_type = {'A': 'aroma', 'L': 'look'}
        parsed_type = args.dataset[-1]
        if parsed_type in ['A', 'L']:
            aspect = aspect_type[parsed_type]
        else:
            err = '[LOAD] Please check dataset name!'
            logger.exception(err)
            raise Exception(err)
        raw_train, raw_test, args = fetch_beerreviews(args=args, root=args.data_path, aspect=aspect, tokenizer=tokenizer)  
        
    elif args.dataset == 'Heart':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario', False)
        _check_and_raise_error(args.eval_type, 'local', 'evaluation type', False)
        split_map, client_datasets, args = fetch_heart(args=args, root=args.data_path, seed=args.seed, test_size=args.test_size)
    
    elif args.dataset == 'Adult':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario', False)
        _check_and_raise_error(args.eval_type, 'local', 'evaluation type', False)
        split_map, client_datasets, args = fetch_adult(args=args, root=args.data_path, seed=args.seed, test_size=args.test_size)
    
    elif args.dataset == 'Cover':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario', False)
        _check_and_raise_error(args.eval_type, 'local', 'evaluation type', False)
        split_map, client_datasets, args = fetch_cover(args=args, root=args.data_path, seed=args.seed, test_size=args.test_size)  
    
    elif args.dataset == 'GLEAM':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario', False)
        _check_and_raise_error(args.eval_type, 'local', 'evaluation type', False)
        split_map, client_datasets, args = fetch_gleam(args=args, root=args.data_path, seed=args.seed, test_size=args.test_size, seq_len=args.seq_len)

    elif args.dataset == 'BraTS':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        transforms = [_get_transform(args, train=True, n_channels=1, to_pil_first=True), _get_transform(args, train=False, n_channels=1, to_pil_first=True), _get_transform(args,target=True, n_channels=1, to_pil_first=True)]
        raw_train, raw_test, args = fetch_brats(args=args, root=args.data_path, transforms=transforms, modality=args.modality)
    
    elif args.dataset == 'MedMNIST':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        transforms = [_get_transform(args, train=True, n_channels=1), _get_transform(args, train=False, n_channels=1), _get_transform(args,target=True, n_channels=1)]
        raw_train, raw_test, args = fetch_medmnist(args=args, root=args.data_path, transforms=transforms, modality=args.modality)
    
    elif args.dataset == 'MTSamples':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        transforms = [partial(tokenizer, padding='max_length', max_length=args.seq_len, truncation=True), partial(tokenizer, padding='max_length', max_length=args.seq_len, truncation=True)]
        raw_train, raw_test, args = fetch_mtsamples(args=args, root=args.data_path, transforms=transforms, modality=args.modality)
    elif args.dataset == 'MedicalAbstracts':
        _check_and_raise_error(args.split_type, 'pre', 'split scenario')
        transforms = [partial(tokenizer, padding='max_length', max_length=args.seq_len, truncation=True), partial(tokenizer, padding='max_length', max_length=args.seq_len, truncation=True)]
        raw_train, raw_test, args = fetch_medabstracts(args=args, root=args.data_path, transforms=transforms, modality=args.modality)
    
    else: # x) for a dataset with no support yet or incorrectly entered...
        err = f'[LOAD] Dataset `{args.dataset}` is not supported or seems incorrectly entered... please check!'
        logger.exception(err)
        raise Exception(err)     
    logger.info(f'[LOAD] ...successfully fetched dataset!')

    if server:
        return (raw_train, raw_test)
    
    ############
    # finalize #
    ############
    # check if global holdout set is required or not
    # if args.eval_type == 'local':
    #     if args.test_size == -1: 
    #         assert raw_test is not None
    #         _raw_test = raw_test
    #     raw_test = None
    # else:
    #     if raw_test is None:
    #         err = f'[LOAD] Dataset `{args.dataset.upper()}` does not support pre-defined validation/test set, which can be used for `global` evluation... please check! (current `eval_type`=`{args.eval_type}`)'
    #         logger.exception(err)
    #         raise AssertionError(err)
            
    # get split indices if None
    if split_map is None:
        logger.info(f'[SIMULATE] Simulate dataset split (split scenario: `{args.split_type.upper()}`)!')
        split_map = simulate_split(args, raw_train)
        logger.info(f'[SIMULATE] ...done simulating dataset split (split scenario: `{args.split_type.upper()}`)!')
    
    # construct client datasets if None
    if client_datasets is None:
        logger.info(f'[SIMULATE] Create client datasets!')
        client_datasets = []
        validation_sets = []
        # with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.K, os.cpu_count() - 1)) as workhorse:
        for idx, sample_indices in TqdmToLogger(
            enumerate(split_map.values()), 
            logger=logger, 
            desc=f'[SIMULATE] ...creating client datasets... ',
            total=len(split_map)
            ):
            res = _construct_dataset(raw_train, idx, sample_indices)
            validation_sets.append(res[1])
            client_datasets.append(res) 
        logger.info(f'[SIMULATE] ...successfully created client datasets!')
        
        ## //when if assigning pre-defined test split as a local holdout set (just divided by the total number of clients)
        if (args.eval_type == 'local'):  
            holdout_sets = torch.utils.data.random_split(raw_test, [int(len(raw_test) / args.K)  for _ in range(args.K)])
            holdout_sets = [SubsetWrapper(holdout_set, f'< {str(idx).zfill(8)} > (test)') for idx, holdout_set in enumerate(holdout_sets)]
            augmented_datasets = []
            for idx, client_dataset in enumerate(client_datasets): 
                augmented_datasets.append((client_dataset[0], holdout_sets[idx], client_dataset[2], client_dataset[3]))
            client_datasets = augmented_datasets
    gc.collect()
    return raw_test, client_datasets, validation_sets    

def load_datasets(args):
    """Fetch and split requested datasets.
    
    Args:
        args: arguments
        
    Returns:
        split_map: {client ID: [assigned sample indices]}
            ex) {0: [indices_1], 1: [indices_2], ... , K: [indices_K]}
        server_testset: (optional) holdout dataset located at the central server, 
        client datasets: [(local training set, local test set)]
            ex) [tuple(local_training_set[indices_1], local_test_set[indices_1]), tuple(local_training_set[indices_2], local_test_set[indices_2]), ...]

    """
    # //assert args.eval_type == 'local', 'PFL setting is required for mm.'

    datasets = args.datasets
    modalities = args.modalities
    data_paths = args.data_paths
    # tasks = args.tasks # For now, one dataset only has one task. Add later if needed.
    num_clients = args.Ks
    num_datasets = len(datasets) - 1 # Server is the last one
    if len(num_clients) == 1:
        num_clients = [num_clients[0]] * num_datasets


    raw_test = None 
    # //Server can't have a test set for different tasks, PFL setting. 
    #  Validation Set from training set
    
    client_datasetss = []
    validata_data = {}
    raw_tests = {}
    for i in range(num_datasets):
        args.dataset = datasets[i]
        args.data_path = data_paths[i]
        args.modality = modalities[i]
        args.K = int(num_clients[i])
        server_dataset, client_datasets, validation_sets = load_dataset(args)
        # validata_data[modalities[i]] = validation_sets[0] if not args.train_as_val else client_datasets[0][0] # Removed for current setting
        raw_tests[datasets[i]] = server_dataset
        if i == 0:
            # raw_tests = [raw_test]
            client_datasetss = client_datasets
        else:
            # raw_tests.append(raw_test)
            for client_dataset in client_datasets:
                client_datasetss.append(client_dataset)

    # Server train and test
    args.dataset = datasets[-1]
    args.data_path = data_paths[-1]
    args.modality = modalities[-1]
    args.K = 1

    server_datasets = load_dataset(args, server=True)
    # validata_data[modalities[i]] = validation_sets[0] if not args.train_as_val else client_datasets[0][0] # Removed for current setting

    args.K = sum([int(num_client) for num_client in num_clients])

    return (server_datasets, raw_tests), client_datasetss    
