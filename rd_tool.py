#!/usr/bin/env python3

from utility import get_time, rd_print
import argparse
import os
import sys
import subprocess
import json
import codecs
import awsremote
import scheduler
import sshslot

# Finding files such as `this_(that)` requires `'` be placed on both
# sides of the quote so the `()` are both captured. Files such as
# `du_Parterre_d'Eau` must be converted into
#`'du_Parterre_d'"'"'Eau'
#                ^^^ Required to make sure the `'` is captured.
def shellquote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"

if 'DAALA_ROOT' not in os.environ:
    rd_print("Please specify the DAALA_ROOT environment variable to use this tool.")
    sys.exit(1)

daala_root = os.environ['DAALA_ROOT']

extra_options = ''
if 'EXTRA_OPTIONS' in os.environ:
    extra_options = os.environ['EXTRA_OPTIONS']
    print(get_time(),'Passing extra command-line options:"%s"' % extra_options)

class RDWork:
    def __init__(self):
        self.failed = False
    def parse(self, stdout, stderr):
        self.raw = stdout
        split = None
        try:
            split = self.raw.decode('utf-8').replace(')',' ').split()
            self.pixels = split[1]
            self.size = split[2]
            self.metric = {}
            self.metric['psnr'] = {}
            self.metric["psnr"][0] = split[6]
            self.metric["psnr"][1] = split[8]
            self.metric["psnr"][2] = split[10]
            self.metric['psnrhvs'] = {}
            self.metric["psnrhvs"][0] = split[14]
            self.metric["psnrhvs"][1] = split[16]
            self.metric["psnrhvs"][2] = split[18]
            self.metric['ssim'] = {}
            self.metric["ssim"][0] = split[22]
            self.metric["ssim"][1] = split[24]
            self.metric["ssim"][2] = split[26]
            self.metric['fastssim'] = {}
            self.metric["fastssim"][0] = split[30]
            self.metric["fastssim"][1] = split[32]
            self.metric["fastssim"][2] = split[34]
            self.metric['ciede2000'] = split[36]
            self.metric['apsnr'] = {}
            self.metric['apsnr'][0] = split[40]
            self.metric['apsnr'][1] = split[42]
            self.metric['apsnr'][2] = split[44]
            self.metric['msssim'] = {}
            self.metric['msssim'][0] = split[48]
            self.metric['msssim'][1] = split[50]
            self.metric['msssim'][2] = split[52]
            self.metric['encodetime'] = split[53]
            self.failed = False
        except IndexError:
            rd_print('Decoding result for '+self.filename+' at quality '+str(self.quality)+' failed!')
            rd_print('stdout:')
            rd_print(stdout.decode('utf-8'))
            rd_print('stderr:')
            rd_print(stderr.decode('utf-8'))
            self.failed = True
    def execute(self, slot):
        work = self
        input_path = '/mnt/media/'+work.set+'/'+work.filename
        slot.start_shell(('DAALA_ROOT="'+daala_root+'" WORK_ROOT="'+slot.work_root+'" x="'+str(work.quality) +
            '" CODEC="'+work.codec+'" EXTRA_OPTIONS="'+work.extra_options +
            '" ' + slot.work_root + '/rd_tool/metrics_gather.sh '+shellquote(input_path)))
        (stdout, stderr) = slot.gather()
        self.parse(stdout, stderr)
    def get_name(self):
        return self.filename + ' with quality ' + str(self.quality)

class ABWork:
    def __init__(self):
        self.failed = False
    def execute(self, slot):
        work = self
        input_path = '/mnt/media/' + work.set + '/' + work.filename

        try:
            slot.start_shell(slot.work_root+'/rd_tool/ab_meta_compare.sh ' + shellquote(str(self.bpp)) + ' ' + shellquote(self.runid) + ' ' + work.set + ' ' + shellquote(input_path) + ' ' + shellquote(self.codec))
            (stdout, stderr) = slot.gather()

            # filename with extension
            if 'video' in work.set:
                filename = input_path.split('/')[-1].rsplit('.', 1)[0] + '.ogv'
            else:
                filename = input_path.split('/')[-1].rsplit('.', 1)[0] + '.png'

            middle = self.runid + '/' + work.set + '/bpp_' + str(self.bpp)

            remote_file = slot.work_root+'/runs/' + middle + '/' + shellquote(filename)
            local_folder = '../runs/' + middle
            local_file = '../runs/' + middle + '/' + filename

            subprocess.Popen(['mkdir', '--parents', local_folder])
            slot.get_file(remote_file, local_file)
            self.failed = False
        except IndexError:
            rd_print('Encoding and copying', filename, 'at bpp', str(self.bpp), 'failed')
            rd_print('stdout:')
            rd_print(stdout.decode('utf-8'))
            rd_print('stderr:')
            rd_print(stderr.decode('utf-8'))
            self.failed = True
    def get_name(self):
        return self.filename + ' with bpp ' + str(self.bpp)

#set up Codec:QualityRange dictionary
quality_presets = {
"daala": [3,5,7,11,16,25,37,55,81,122,181],
"x264": list(range(1,52,5)),
"x265": list(range(5,52,5)),
"x265-rt": list(range(5,52,5)),
"vp8": list(range(12,64,5)),
"vp9": list(range(12,64,5)),
"vp10": list(range(12,64,5)),
"vp10-rt": list(range(12,64,5)),
"av1": [8,20,32,43,55,63],
"av1-rt": [8,20,32,43,55,63],
"thor": list(range(7,43,3)),
"thor-rt": list(range(7,43,3))
}

work_items = []

#load all the different sets and their filenames
video_sets_f = codecs.open('sets.json','r',encoding='utf-8')
video_sets = json.load(video_sets_f)

parser = argparse.ArgumentParser(description='Collect RD curve data.')
parser.add_argument('set',metavar='Video set name',nargs='+')
parser.add_argument('-codec',default='daala')
parser.add_argument('-prefix',default='.')
parser.add_argument('-awsgroup', default='Daala')
parser.add_argument('-machines', default=14)
parser.add_argument('-mode', default='metric')
parser.add_argument('-runid', default=get_time())
parser.add_argument('-seed')
parser.add_argument('-bpp')
parser.add_argument('-qualities',nargs='+')
parser.add_argument('-machineconf')

args = parser.parse_args()

aws_group_name = args.awsgroup

#check we have the codec in our codec-qualities dictionary
if args.codec not in quality_presets:
    rd_print('Invalid codec. Valid codecs are:')
    for q in quality_presets:
        rd_print(q)
    sys.exit(1)

if args.qualities:
    quality = args.qualities
else:
    quality = quality_presets[args.codec]

#check we have the set name in our sets-filenames dictionary
if args.set[0] not in video_sets:
    rd_print('Specified invalid set '+args.set[0]+'. Available sets are:')
    for video_set in video_sets:
        rd_print(video_set)
    sys.exit(1)

total_num_of_jobs = len(video_sets[args.set[0]]['sources']) * len(quality)

#a logging message just to get the regex progress bar on the AWCY site started...
rd_print('0 out of',total_num_of_jobs,'finished.')

#how many AWS instances do we want to spin up?
#The assumption is each machine can deal with 18 threads,
#so up to 18 jobs, use 1 machine, then up to 64 use 2, etc...
num_instances_to_use = (31 + total_num_of_jobs) // 18

#...but lock AWS to a max number of instances
max_num_instances_to_use = int(args.machines)

if num_instances_to_use > max_num_instances_to_use:
    rd_print('Ideally, we should use',num_instances_to_use,
        'instances, but the max is',max_num_instances_to_use,'.')
    num_instances_to_use = max_num_instances_to_use

machines = []
if args.machineconf:
    machineconf = json.load(open(args.machineconf, 'r'))
    for m in machineconf:
        machines.append(sshslot.Machine(m['host'],m['user'],m['cores'],m['work_root'],str(m['port'])))
else:
    while not machines:
        machines = awsremote.get_machines(num_instances_to_use, aws_group_name)

slots = []
#set up our instances and their free job slots
for machine in machines:
    machine.setup(args.codec)
    slots.extend(machine.get_slots())


#Make a list of the bits of work we need to do.
#We pack the stack ordered by filesize ASC, quality ASC (aka. -v DESC)
#so we pop the hardest encodes first,
#for more efficient use of the AWS machines' time.

video_filenames = video_sets[args.set[0]]['sources']

if args.mode == 'metric':
    for filename in video_filenames:
        for q in sorted(quality, reverse = True):
            work = RDWork()
            work.quality = q
            work.codec = args.codec
            work.set = args.set[0]
            work.filename = filename
            work.extra_options = extra_options
            work_items.append(work)
elif args.mode == 'ab':
    if video_sets[args.set[0]]['type'] == 'video':
        bits_per_pixel = [0.01]
        print("mode `ab` isn't supported for videos. Skipping.")
    else:
        bits_per_pixel = [x/10.0 for x in range(1, 11)]
        for filename in video_filenames:
            for bpp in bits_per_pixel:
                work = ABWork()
                work.bpp = bpp
                work.codec = args.codec
                work.runid = str(args.runid)
                work.set = args.set[0]
                work.filename = filename
                work.extra_options = extra_options
                work_items.append(work)
else:
    print('Unsupported -mode parameter.')
    sys.exit(1)

if len(slots) < 1:
    rd_print('All AWS machines are down.')
    sys.exit(1)

work_done = scheduler.run(work_items, slots)

if args.mode == 'metric':
    rd_print('Logging results...')
    work_done.sort(key=lambda work: work.quality)
    for work in work_done:
        if not work.failed:
            f = open((args.prefix+'/'+work.filename+'-daala.out').encode('utf-8'),'a')
            f.write(str(work.quality)+' ')
            f.write(str(work.pixels)+' ')
            f.write(str(work.size)+' ')
            f.write(str(work.metric['psnr'][0])+' ')
            f.write(str(work.metric['psnrhvs'][0])+' ')
            f.write(str(work.metric['ssim'][0])+' ')
            f.write(str(work.metric['fastssim'][0])+' ')
            f.write(str(work.metric['ciede2000'])+' ')
            f.write(str(work.metric['psnr'][1])+' ')
            f.write(str(work.metric['psnr'][2])+' ')
            f.write(str(work.metric['apsnr'][0])+' ')
            f.write(str(work.metric['apsnr'][1])+' ')
            f.write(str(work.metric['apsnr'][2])+' ')
            f.write(str(work.metric['msssim'][0])+' ')
            f.write(str(work.metric['encodetime'])+' ')
            f.write('\n')
            f.close()
    subprocess.call('OUTPUT="'+args.prefix+'/'+'total" "'+sys.path[0]+'/rd_average.sh" "'+args.prefix+'/*.out"',
      shell=True)

rd_print('Done!')
