from __future__ import print_function
import numpy as np
import h5py
import os, sys, time, argparse
from hdf5_deeplearn_utils import calc_data_mean, calc_data_std, build_train_test_split, resize_data_into_new_key

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--filename')
    parser.add_argument('--rewrite', default=0, type=int)
    parser.add_argument('--train_length', default=5*60, type=float)
    parser.add_argument('--test_length', default=2*60, type=float)
    parser.add_argument('--skip_mean_std', default=0, type=int)
    parser.add_argument('--new_height', default=60, type=int)
    parser.add_argument('--new_width', default=80, type=int)
    args = parser.parse_args()

    # Set new resize
    new_size = (args.new_height, args.new_width)

    dataset = h5py.File(args.filename, 'a')
    print('Calculating train/test split...')
    sys.stdout.flush()
    build_train_test_split(dataset, train_div=args.train_length, test_div=args.test_length, force=args.rewrite)

    if "aps_frame" in dataset:
        new_aps_key = '{}_{}x{}'.format('aps_frame', new_size[0], new_size[1])
        print('Resizing APS frames to {}...'.format(new_aps_key))
        sys.stdout.flush()
        start_time = time.time()
        resize_data_into_new_key(dataset, 'aps_frame', new_aps_key, new_size)
        print('Finished in {}s.'.format(time.time()-start_time))

        if not args.skip_mean_std:
            print('Calculating APS frame mean...')
            sys.stdout.flush()
            start_time = time.time()
            calc_data_mean(dataset, new_aps_key, force=args.rewrite)
            print('Finished in {}s.'.format(time.time()-start_time))

            print('Calculating APS frame std...')
            sys.stdout.flush()
            start_time = time.time()
            calc_data_std(dataset, new_aps_key, force=args.rewrite)
            print('Finished in {}s.'.format(time.time()-start_time))

    if "dvs_frame" in dataset:
        new_dvs_key = '{}_{}x{}'.format('dvs_frame', new_size[0], new_size[1])
        print('Resizing DVS frames to {}...'.format(new_dvs_key))
        sys.stdout.flush()
        start_time = time.time()
        resize_data_into_new_key(dataset, 'dvs_frame', new_dvs_key, new_size)
        print('Finished in {}s.'.format(time.time()-start_time))

        if not args.skip_mean_std:
            print('Calculating DVS frame mean...')
            sys.stdout.flush()
            start_time = time.time()
            calc_data_mean(dataset, new_dvs_key, force=args.rewrite)
            print('Finished in {}s.'.format(time.time()-start_time))
            sys.stdout.flush()

            print('Calculating DVS frame std...')
            sys.stdout.flush()
            start_time = time.time()
            calc_data_std(dataset, new_dvs_key, force=args.rewrite)
            print('Finished in {}s.'.format(time.time()-start_time))

    print('Done.  Preprocessing complete.')
    filesize = os.path.getsize(args.filename)
    print('Final size: {:.1f}MiB to {}.'.format(filesize/1024**2, args.filename))
