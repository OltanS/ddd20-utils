#!/usr/bin/python

'''
Experimental viewer for DAVIS + OpenXC data
Author: J. Binas <jbinas@gmail.com>, 2017

This software is released under the
GNU LESSER GENERAL PUBLIC LICENSE Version 3.

Usage:
 Play a file from the beginning:
 $ ./view.py <recorded_file.hdf5>

 Play a file, starting at X percent:
 $ ./view.py <recorded_file.hdf5> X%

 Play a file starting at second X
 $ ./view.py <recorded_file.hdf5> Xs
'''

from __future__ import print_function
import numpy as np
import h5py
import cv2
import time, sys
import Queue
import multiprocessing as mp
from interfaces.caer import DVS_SHAPE, unpack_header, unpack_data
from datasets import CHUNK_SIZE


VIEW_DATA = {
        'dvs',
        'steering_wheel_angle',
        'engine_speed',
        'accelerator_pedal_position',
        'brake_pedal_status',
        'vehicle_speed',
        }


# this changed in version 3
CV_AA = cv2.LINE_AA if int(cv2.__version__[0]) > 2 else cv2.CV_AA


def _flush_q(q):
    ''' flush queue '''
    while True:
        try:
            q.get(timeout=1e-3)
        except Queue.Empty:
            if q.empty():
                break


class HDF5Stream(mp.Process):
    def __init__(self, filename, tables, bufsize=64):
        super(HDF5Stream, self).__init__()
        self.f = h5py.File(filename, 'r')
        self.tables = tables
        self.q = {k: mp.Queue(bufsize) for k in self.tables}
        self.run_search = mp.Event()
        self.exit = mp.Event()
        self.done = mp.Event()
        self.skip_to = mp.Value('L', 0)
        self._init_count()
        self._init_time()
        self.daemon = True
        self.start()

    def run(self):
        while self.blocks_rem and not self.exit.is_set():
            blocks_read = 0
            for k in self.blocks_rem.keys():
                if self.q[k].full():
                    time.sleep(1e-6)
                    continue
                i = self.block_offset[k]
                self.q[k].put(self.f[k]['data'][i*CHUNK_SIZE:(i+1)*CHUNK_SIZE])
                self.block_offset[k] += 1
                if self.blocks_rem[k].value:
                    self.blocks_rem[k].value -= 1
                else:
                    self.blocks_rem.pop(k)
                blocks_read += 1
            if not blocks_read:
                time.sleep(1e-6)
            if self.run_search.is_set():
                self._search()
        self.f.close()
        print('closed input file')
        while not self.exit.is_set():
            time.sleep(1e-3)
        #print('[DEBUG] flushing stream queues')
        for k in self.q:
            #print('[DEBUG] flushing', k)
            _flush_q(self.q[k])
            self.q[k].close()
            self.q[k].join_thread()
        #print('[DEBUG] flushed all stream queues')
        self.done.set()
        print('stream done')

    def get(self, k, block=True, timeout=None):
        return self.q[k].get(block, timeout)

    def _init_count(self, offset={}):
        self.block_offset = {k: offset.get(k,0) / CHUNK_SIZE \
                for k in self.tables}
        self.size = {k: len(self.f[k]['data']) - v * CHUNK_SIZE \
                for k, v in self.block_offset.items()}
        self.blocks = {k: v / CHUNK_SIZE for k, v in self.size.items()}
        self.blocks_rem = {k: mp.Value('L', v) for k, v in self.blocks.items() if v}

    def _init_time(self):
        self.ts_start = {}
        self.ts_stop = {}
        self.ind_stop = {}
        for k in self.tables:
            ts_start = self.f[k]['timestamp'][self.block_offset[k]*CHUNK_SIZE]
            self.ts_start[k] = mp.Value('L', ts_start)
            b = self.block_offset[k] + self.blocks_rem[k].value - 1
            while \
                b > self.block_offset[k] and \
                self.f[k]['timestamp'][b*CHUNK_SIZE] == 0:
                    b -= 1
            print(k, 'final block:', b)
            self.ts_stop[k] = mp.Value('L',
                    self.f[k]['timestamp'][(b + 1) * CHUNK_SIZE - 1])
            self.ind_stop[k] = b

    def init_search(self, t):
        ''' start streaming from given time point '''
        if self.run_search.is_set():
            return
        self.skip_to.value = np.uint64(t)
        self.run_search.set()

    def _search(self):
        t = self.skip_to.value
        offset = {k: self._bsearch_by_timestamp(k, t) for k in self.tables}
        for k in self.tables:
            _flush_q(self.q[k])
        self._init_count(offset)
        #self._init_time()
        self.run_search.clear()

    def _bsearch_by_timestamp(self, k, t):
        ''' performs binary search on timestamp, returns closest block index '''
        l, r = 0, self.ind_stop[k]
        print('searching', k, t)
        while True:
            if r - l < 2:
                print('selecting block', l)
                return l * CHUNK_SIZE
            if self.f[k]['timestamp'][(l + (r - l) / 2) * CHUNK_SIZE] > t:
                r = l + (r - l) / 2
            else:
                l += (r - l) / 2


class MergedStream(mp.Process):
    ''' Unpacks and merges data from HDF5 stream '''
    def __init__(self, fbuf, bufsize=256):
        super(MergedStream, self).__init__()
        self.fbuf = fbuf
        self.ts_start = self.fbuf.ts_start
        self.ts_stop = self.fbuf.ts_stop
        self.q = mp.Queue(bufsize)
        self.run_search = mp.Event()
        self.skip_to = mp.Value('L', 0)
        self._init_state()
        self.done = mp.Event()
        self.fetched_all = mp.Event()
        self.exit = mp.Event()
        self.daemon = True
        self.start()

    def run(self):
        while self.blocks_rem and not self.exit.is_set():
            #find next event
            if self.q.full():
                time.sleep(1e-4)
                continue
            next_k = min(self.current_ts, key=self.current_ts.get)
            self.q.put((self.current_ts[next_k], self.current_dat[next_k]))
            self._inc_current(next_k)
            # get new blocks if necessary
            for k in {k for k in self.blocks_rem if self.i[k] == CHUNK_SIZE}:
                self.current_blk[k] = self.fbuf.get(k)
                self.i[k] = 0
                if self.blocks_rem[k]:
                    self.blocks_rem[k] -= 1
                else:
                    self.blocks_rem.pop(k)
                    self.current_ts.pop(k)
            if self.run_search.is_set():
                self._search()
        self.fetched_all.set()
        self.fbuf.exit.set()
        while not self.fbuf.done.is_set():
            time.sleep(1)
            #print('[DEBUG] waiting for stream process')
        while not self.exit.is_set():
            time.sleep(1)
            #print('[DEBUG] waiting for merger process')
        _flush_q(self.q)
        #print('[DEBUG] flushed merger q ->', self.q.qsize())
        self.q.close()
        self.q.join_thread()
        #print('[DEBUG] joined merger q')
        self.done.set()

    def close(self):
        self.exit.set()

    def _init_state(self):
        keys = self.fbuf.blocks_rem.keys()
        self.blocks_rem = {k: self.fbuf.blocks_rem[k].value for k in keys}
        self.current_blk = {k: self.fbuf.get(k) for k in keys}
        self.i = {k: 0 for k in keys}
        self.current_dat = {}
        self.current_ts = {}
        for k in keys:
            self._inc_current(k)

    def _inc_current(self, k):
        ''' get next event of given type and increment row pointer '''
        row = self.current_blk[k][self.i[k]]
        if k == 'dvs':
            ts, d = caer_event_from_row(row)
        else: # vi event
            ts = row[0] * 1e-6
            d = {'etype': k, 'timestamp': row[0], 'data': row[1]}
        if not ts and k in self.current_ts:
            self.current_ts.pop(k)
            self.blocks_rem.pop(k)
            return False
        self.current_ts[k], self.current_dat[k] = ts, d
        self.i[k] += 1

    def get(self, block=False):
        return self.q.get(block)

    @property
    def has_data(self):
        return not (self.fetched_all.is_set() and self.q.empty())

    @property
    def tmin(self):
        return self.ts_start['dvs'].value

    @property
    def tmax(self):
        return self.ts_stop['dvs'].value

    def search(self, t, block=True):
        if self.run_search.is_set():
            return
        self.skip_to.value = np.uint64(t)
        self.run_search.set()

    def _search(self):
        self.fbuf.init_search(self.skip_to.value)
        while self.fbuf.run_search.is_set():
            time.sleep(1e-6)
        _flush_q(self.q)
        self._init_state()
        self.q.put((0, {'etype': 'timestamp_reset'}))
        self.run_search.clear()


class Interface(object):
    def __init__(self,
            tmin=0, tmax=0,
            search_callback=None,
            update_callback=None,
            create_callback=None,
            destroy_callback=None):
        self.tmin, self.tmax = tmin, tmax
        self.search_callback = search_callback
        self.update_callback = update_callback
        self.create_callback = create_callback
        self.destroy_callback = destroy_callback

    def _set_t(self, t):
        self.t_now = int(t - self.tmin)
        if self.update_callback is not None:
            self.update_callback(t)

    def close(self):
        if self.close_callback is not None:
            self.close_callback


class Viewer(Interface):
    ''' Simple visualizer for events '''
    def __init__(self, max_fps=40, zoom=1, **kwargs):
        super(Viewer, self).__init__(**kwargs)
        self.zoom = zoom
        cv2.namedWindow('frame')
        cv2.namedWindow('polarity')
        cv2.moveWindow('frame', 400, 300)
        cv2.moveWindow('polarity', 400 + int(348 * self.zoom), 300)
        self.set_fps(max_fps)
        self.pol_img = 0.5 * np.ones(DVS_SHAPE)
        self.t_now = 0
        self.t_pre = {}
        self.count = {}
        self.cache = {}
        self.font = cv2.FONT_HERSHEY_SIMPLEX

    def set_fps(self, max_fps):
        self.min_dt = 1. / max_fps

    def show(self, d, t=None):
        ''' receive and handle single event '''
        if 'etype' not in d: #
            d['etype'] = d['name']
        etype = d['etype']
        if not self.t_pre.get(etype):
            self.t_pre[etype] = -1
        self.count[etype] = self.count.get(etype, 0) + 1
        if etype == 'frame_event' and time.time() - self.t_pre[etype] > self.min_dt:
            if 'data' not in d: #
                unpack_data(d)
            img = (d['data'] / 256).astype(np.uint8)
            self._plot_steering_wheel(img)
            self._print(img, (50,220), 'accelerator_pedal_position', '%')
            self._print(img, (100,220), 'brake_pedal_status', 'brake', True)
            self._print(img, (200,220), 'vehicle_speed', 'km/h')
            self._print(img, (300,220), 'engine_speed', 'rpm')
            if t is not None:
                self._plot_timeline(img)
            if self.zoom != 1:
                img = cv2.resize(img, None, fx=self.zoom, fy=self.zoom, interpolation=cv2.INTER_CUBIC)
            cv2.imshow('frame', img)
            cv2.waitKey(1)
            self.t_pre[etype] = time.time()
        elif etype == 'polarity_event':
            if 'data' not in d: #
                unpack_data(d)
            self.pol_img[d['data'][:,2], d['data'][:,1]] = d['data'][:,3]
            if time.time() - self.t_pre[etype] > self.min_dt:
                if self.zoom != 1:
                    self.pol_img = cv2.resize(
                            self.pol_img, None,
                            fx=self.zoom, fy=self.zoom,
                            interpolation=cv2.INTER_CUBIC)
                cv2.imshow('polarity', self.pol_img)
                cv2.waitKey(1)
                self.pol_img = 0.5 * np.ones(DVS_SHAPE)
                self.t_pre[etype] = time.time()
        elif etype in VIEW_DATA:
            if 'data' not in d: #
                d['data'] = d['value']
            self.cache[etype] = d['data']
            self.t_pre[etype] = time.time()
        if t is not None:
            self._set_t(t)

    def _plot_steering_wheel(self, img):
        if 'steering_wheel_angle' not in self.cache:
            return
        c, r = (173, 130), 65 #center, radius
        a = self.cache['steering_wheel_angle']
        a_rad = + a / 180. * np.pi + np.pi / 2
        t = (c[0] + int(np.cos(a_rad) * r), c[1] - int(np.sin(a_rad) * r))
        cv2.line(img, c, t, 255, 2, CV_AA)
        cv2.circle(img, c, r, 255, 1, CV_AA)
        cv2.line(img, (c[0]-r+5, c[1]), (c[0]-r, c[1]), 255, 1, CV_AA)
        cv2.line(img, (c[0]+r-5, c[1]), (c[0]+r, c[1]), 255, 1, CV_AA)
        cv2.line(img, (c[0], c[1]-r+5), (c[0], c[1]-r), 255, 1, CV_AA)
        cv2.line(img, (c[0], c[1]+r-5), (c[0], c[1]+r), 255, 1, CV_AA)
        cv2.putText(img, '%0.1f deg' % a, (c[0]-35, c[1]+30), self.font, 0.4, 255, 1, CV_AA)

    def _print(self, img, pos, name, unit, autohide=False):
        if name not in self.cache:
            return
        v = self.cache[name]
        if autohide and v == 0:
            return
        cv2.putText(img, '%d %s' % (v, unit), (pos[0]-40, pos[1]+20), self.font, 0.4, 255, 1, CV_AA)

    def _plot_timeline(self, img):
        pos = (50, 10)
        p = int(346 * self.t_now / (self.tmax - self.tmin))
        cv2.line(img, (0, 2), (p, 2), 255, 1, CV_AA)
        cv2.putText(img, '%d s' % self.t_now, (pos[0]-40, pos[1]+20), self.font, 0.4, 255, 1, CV_AA)

    def close(self):
        cv2.destroyAllWindows()


class Controller(Interface):
    def __init__(self, filename, **kwargs):
        super(Controller, self).__init__(**kwargs)
        cv2.namedWindow('control')
        cv2.moveWindow('control', 400, 698)
        self.f = h5py.File(filename, 'r')
        self.tmin, self.tmax = self._get_ts()
        self.len = int(self.tmax - self.tmin) + 1
        img = np.zeros((100, self.len))
        self.plot_pixels(img, 'headlamp_status', 0, 10)
        self.plot_line(img, 'steering_wheel_angle', 20, 30)
        self.plot_line(img, 'vehicle_speed', 69, 30)
        self.width = 978
        self.img = cv2.resize(img, (self.width,100),
                interpolation=cv2.INTER_NEAREST)
        cv2.setMouseCallback('control', self._set_search)
        self.t_pre = 0
        self.update(0)
        self.f.close()

    def update(self, t):
        self._set_t(t)
        t = int(float(self.width) / self.len * (t - self.tmin))
        if t == self.t_pre:
            return
        self.t_pre = t
        img = self.img.copy()
        img[:,:t+1] = img[:,:t+1] * 0.5 + 0.5
        cv2.imshow('control', img)
        cv2.waitKey(1)

    def plot_line(self, img, name, offset, height):
        x, y = self.get_xy(name)
        if x is None:
            return
        y -= y.min()
        y = y / y.max() * height
        x = x.clip(0, self.len - 1)
        img[offset+height-y.astype(int), x] = 1

    def plot_pixels(self, img, name, offset=0, height=1):
        x, y = self.get_xy(name)
        if x is None:
            return
        img[offset:offset+height, x] = y

    def _set_search(self, event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        t = self.len * 1e6 * x / float(self.width) + self.tmin * 1e6
        self._search_callback(t)

    def _get_ts(self):
        ts = self.f['dvs']['timestamp']
        tmin = ts[0]
        i = -1
        while ts[i] == 0:
            i -= 1
        tmax = ts[i]
        print('tmin/tmax', tmin, tmax)
        return int(tmin * 1e-6), int(tmax * 1e-6)

    def get_xy(self, name):
        d = self.f[name]['data']
        print('name', name)
        gtz_ids = d[:, 0] > 0
        if not gtz_ids.any():
            return None, 0
        gtz = d[gtz_ids, :]
        return (gtz[:, 0] * 1e-6 - self.tmin).astype(int), gtz[:, 1]


def caer_event_from_row(row):
    '''
    Takes binary dvs data as input,
    returns unpacked event data or False if event type does not exist.
    '''
    sys_ts, head, body = (v.tobytes() for v in row)
    if not sys_ts:
        # rows with 0 timestamp do not contain any data
        return 0, False
    d = unpack_header(head)
    d['dvs_data'] = body
    return int(sys_ts) * 1e-6, unpack_data(d)



if __name__ == '__main__':

    fname = sys.argv[1].strip()
    # cv2.startWindowThread()
    c = Controller(fname,)
    m = MergedStream(HDF5Stream(fname, VIEW_DATA))
    c._search_callback = m.search
    t = time.time()
    t_pre = 0
    t_offset = 0
    v = Viewer(tmin=m.tmin * 1e-6, tmax=m.tmax * 1e-6,
            zoom=1.41, update_callback=c.update)
    print('recording duration', (m.tmax - m.tmin) * 1e-6, 's')
    # direct skip by command line arg
    if len(sys.argv) == 3:
        n_, type_ = sys.argv[2][:-1].strip(), sys.argv[2][-1].strip()
        if type_ == '%':
            m.search((m.tmax - m.tmin) * 1e-2 * float(n_) + m.tmin)
        else:
            m.search(float(n_) * 1e6 + m.tmin)
    # run main loop
    ts_reset = False
    while m.has_data:
        try:
            sys_ts, d = m.get()
        except Queue.Empty:
            continue
        if not d:
            continue
        if d['etype'] == 'timestamp_reset':
            ts_reset = True
            continue
        if not d['etype'] in {'frame_event', 'polarity_event'}:
            v.show(d)
            continue
        if d['timestamp'] < t_pre:
            print('[WARN] negative dt detected!')
        t_pre = d['timestamp']
        if ts_reset:
            print('resetting timestamp')
            t_offset = 0
            ts_reset = False
        if not t_offset:
            t_offset = time.time() - d['timestamp']
            print('setting offset', t_offset)
        t_sleep = max(d['timestamp'] - time.time() + t_offset, 0)
        time.sleep(t_sleep)
        v.show(d, sys_ts)
        if time.time() - t > 1:
            t = time.time()
            print('fps:\n', '\n'.join(['  %s %s' % (k.ljust(20), v_) for k, v_ in v.count.items()]))
            v.count = {k: 0 for k in v.count}


