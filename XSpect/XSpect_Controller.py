import h5py
import psana
import numpy as np
import matplotlib.pyplot as plt
from scipy.ndimage import rotate
from scipy.interpolate import interp1d
from scipy.optimize import curve_fit, minimize
import multiprocessing
import os
from functools import partial
import time
import sys
from datetime import datetime
import argparse
from XSpect.XSpect_Analysis import *
from XSpect.XSpect_Analysis import spectroscopy_run
from multiprocessing import Pool
from tqdm import tqdm

class BatchAnalysis:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.status = []
        self.status_datetime = []
        self.filters = []
        self.keys = []
        self.friendly_names = []
        self.runs = []
        self.run_shots = {}
        self.run_shot_ranges = {}
        self.analyzed_runs = []

    def update_status(self, update):
        self.status.append(update)
        self.status_datetime.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if self.verbose:
            print(update)

    def run_parser(self, run_array):
        self.update_status("Parsing run array.")
        run_string = ' '.join(run_array)
        runs = []
        for run_range in run_string.split():
            if '-' in run_range:
                start, end = map(int, run_range.split('-'))
                runs.extend(range(start, end+1))
            else:
                try:
                    runs.append(int(run_range))
                except ValueError:
                    raise ValueError(f"Invalid input: {run_range}")

        self.runs = runs

    def add_filter(self, shot_type, filter_key, threshold):
        self.update_status(f"Adding filter: Shot Type={shot_type}, Filter Key={filter_key}, Threshold={threshold}")
        if shot_type not in ['xray', 'laser', 'simultaneous']:
            raise ValueError('Only options for shot type are xray, laser, or simultaneous.')
        self.filters.append({'FilterType': shot_type, 'FilterKey': filter_key, 'FilterThreshold': threshold})

    def set_key_aliases(self, keys=['tt/ttCorr', 'epics/lxt_ttc', 'enc/lasDelay', 'ipm4/sum', 'tt/AMPL', 'epix_2/ROI_0_area'],
                        names=['time_tool_correction', 'lxt_ttc', 'encoder', 'ipm', 'time_tool_ampl', 'epix']):
        self.update_status("Setting key aliases.")
        self.keys = keys
        self.friendly_names = names

    def primary_analysis_loop(self, experiment, verbose=False):
        self.update_status(f"Starting primary analysis loop with experiment={experiment}, verbose={verbose}.")
        analyzed_runs = []
        for run in self.runs:
            analyzed_runs.append(self.primary_analysis(experiment, run, verbose))
        self.analyzed_runs = analyzed_runs
        self.update_status("Primary analysis loop completed.")

    def primary_analysis_parallel_loop(self, cores, experiment, verbose=False):
        self.update_status(f"Starting parallel analysis loop with cores={cores}, experiment={experiment}, verbose={verbose}.")
        pool = Pool(processes=cores)
        analyzed_runs = []

        def callback(result):
            analyzed_runs.append(result)

        with tqdm(total=len(self.runs), desc="Processing Runs", unit="Run") as pbar:
            for analyzed_run in pool.imap(partial(self.primary_analysis, experiment=experiment, verbose=verbose), self.runs):
                pbar.update(1)
                analyzed_runs.append(analyzed_run)

        pool.close()
        pool.join()

        analyzed_runs = [analyzed_run for analyzed_run in sorted(analyzed_runs, key=lambda x: (x.run_number, x.end_index))]
        self.analyzed_runs = analyzed_runs
        self.update_status("Parallel analysis loop completed.")
        

    def primary_analysis(self):
        raise AttributeError('The primary_analysis must be implemented by the child classes.')

    def parse_run_shots(self, experiment, verbose=False):
        self.update_status("Parsing run shots.")
        run_shots_dict = {}
        for run in self.runs:
            f = spectroscopy_run(experiment, run, verbose=verbose, end_index=self.end_index)
            f.get_run_shot_properties()
            run_shots_dict[run] = f.total_shots
        self.run_shots = run_shots_dict
        self.update_status("Run shots parsed.")

    def break_into_shot_ranges(self, increment):
        self.update_status(f"Breaking into shot ranges with increment {increment}.")
        run_shot_ranges_dict = {}
        for run, total_shots in self.run_shots.items():
            run_shot_ranges = []
            min_index = 0
            if self.end_index is not None and self.end_index!=-1:
                total_shots=min(self.end_index, total_shots)
            while min_index < total_shots:
                max_index = min_index + increment - 1 if min_index + increment - 1 < total_shots else total_shots - 1
                run_shot_ranges.append((min_index, max_index))
                min_index += increment
            run_shot_ranges_dict[run] = run_shot_ranges
        self.run_shot_ranges = run_shot_ranges_dict
        self.update_status("Shot ranges broken.")
        # Convert dictionary items to a list of tuples

        flat_list = [(run, (shot_range[0], shot_range[1])) for run, shot_ranges in run_shot_ranges_dict.items() for shot_range in shot_ranges]

        result_array = np.array(flat_list,dtype=object)
        self.run_shot_ranges=result_array

    def primary_analysis_parallel_range(self, cores, experiment, increment, start_index=None, end_index=None, verbose=False):

        
        self.update_status("Starting parallel analysis with shot ranges.")
        self.parse_run_shots(experiment, verbose)
        self.break_into_shot_ranges(increment)

        analyzed_runs = []
        total_runs = len(self.run_shot_ranges)

        with Pool(processes=cores) as pool, tqdm(total=total_runs, desc="Processing", unit="Shot_Batch") as pbar:
            run_shot_ranges = self.run_shot_ranges

            def callback(result):
                nonlocal pbar
                pbar.update(1)
                analyzed_runs.append(result)

            for run_shot in run_shot_ranges:
                run, shot_ranges = run_shot
                pool.apply_async(self.primary_analysis_range, (experiment, run, shot_ranges, verbose), callback=callback)
            pool.close()
            pool.join()
        self.analyzed_runs = analyzed_runs
        analyzed_runs = [analyzed_run for analyzed_run in sorted(analyzed_runs, key=lambda x: (x.run_number, x.end_index))]
        self.analyzed_runs = analyzed_runs
        self.update_status("Parallel analysis with shot ranges completed.")


def analyze_single_run(args):
    obj,experiment, run, shot_ranges, verbose = args
    return obj.primary_analysis_range(experiment, run, shot_ranges, verbose)



class XESBatchAnalysis(BatchAnalysis):
    def __init__(self):
        super().__init__()
        self.xes_line='kbeta'
        self.pixels_to_patch=[351,352,529,530,531]
        self.crystal_detector_distance=50.6
        self.crystal_d_space=0.895
        self.crystal_radius=250
        self.adu_cutoff=3.0
        self.rois=[[0,None]]
        self.mintime=-2.0
        self.maxtime=10.0
        self.numpoints=240
        self.time_bins=np.linspace(self.mintime,self.maxtime,self.numpoints)
        self.filters=[]
        self.key_epix=['epix_2/ROI_0_area']
        self.friendly_name_epix=['epix']
        self.angle=0.0
        self.end_index=-1
        self.start_index=0
 
    
    def primary_analysis(self,experiment,run,verbose=False,start_index=None,end_index=None):
        if end_index==None:
            end_index=self.end_index
        if start_index==None:
            try:
                start_index=self.start_index
            except AttributeError:
                start_index=0
        f=spectroscopy_run(experiment,run,verbose=verbose,start_index=start_index,end_index=end_index)
        f.get_run_shot_properties()
        f.load_run_keys(self.keys,self.friendly_names)
        f.load_run_key_delayed(self.key_epix,self.friendly_name_epix)
        analysis=XESAnalysis()
        analysis.reduce_detector_spatial(f,'epix', rois=self.rois,adu_cutoff=self.adu_cutoff)
        analysis.filter_detector_adu(f,'epix',adu_threshold=self.adu_cutoff)
        analysis.union_shots(f,'epix_ROI_1',['simultaneous','laser'])
        analysis.separate_shots(f,'epix_ROI_1',['xray','laser'])
        self.bins=np.linspace(self.mintime,self.maxtime,self.numpoints)
        analysis.time_binning(f,self.bins)
        analysis.union_shots(f,'timing_bin_indices',['simultaneous','laser'])
        analysis.separate_shots(f,'timing_bin_indices',['xray','laser'])
        analysis.reduce_detector_temporal(f,'epix_ROI_1_simultaneous_laser','timing_bin_indices_simultaneous_laser',average=False)
        analysis.reduce_detector_temporal(f,'epix_ROI_1_xray_not_laser','timing_bin_indices_xray_not_laser',average=False)
        analysis.normalize_xes(f,'epix_ROI_1_simultaneous_laser_time_binned')
        analysis.normalize_xes(f,'epix_ROI_1_xray_not_laser_time_binned')   
        analysis.pixels_to_patch=self.pixels_to_patch
        f.close_h5()
        analysis.make_energy_axis(f,f.epix_ROI_1.shape[1],A=self.crystal_detector_distance,R=self.crystal_radius,d=self.crystal_d_space)
        for fil in self.filters:
            analysis.filter_shots(f,fil['FilterType'],fil['FilterKey'],fil['FilterThreshold'])                                                                      
        
        return f
    
class XESBatchAnalysisRotation(XESBatchAnalysis):
    def __init__(self):
        super().__init__()
    
    def primary_analysis_static_parallel_loop(self, cores, experiment, verbose=False):
        self.update_status(f"Starting parallel analysis loop with cores={cores}, experiment={experiment}, verbose={verbose}.")
        pool = Pool(processes=cores)
        analyzed_runs = []

        def callback(result):
            analyzed_runs.append(result)

        with tqdm(total=len(self.runs), desc="Processing Runs", unit="Run") as pbar:
            for analyzed_run in pool.imap(partial(self.primary_analysis_static, experiment=experiment, verbose=verbose), self.runs):
                pbar.update(1)
                analyzed_runs.append(analyzed_run)

        pool.close()
        pool.join()

        analyzed_runs = [analyzed_run for analyzed_run in sorted(analyzed_runs, key=lambda x: (x.run_number, x.end_index))]
        self.analyzed_runs = analyzed_runs
        self.update_status("Parallel analysis loop completed.")

    def primary_analysis_static(self, run, experiment, verbose=False, start_index=None,end_index=None):
        if end_index==None:
            end_index=self.end_index
        if start_index==None:
            try:
                start_index=self.start_index
            except AttributeError:
                start_index=0
        self.end_index=end_index
        self.start_index=start_index
        f=spectroscopy_run(experiment,run,verbose=verbose,start_index=start_index,end_index=end_index)
        f.get_run_shot_properties()
        f.load_run_keys(self.keys,self.friendly_names)
        f.load_run_key_delayed(self.key_epix,self.friendly_name_epix)
        analysis=XESAnalysis()
        analysis.pixels_to_patch=self.pixels_to_patch
        analysis.filter_detector_adu(f,'epix',adu_threshold=self.adu_cutoff)
        analysis.patch_pixels(f,'epix',axis=1)
        # analysis.patch_pixels_1d(f,'epix')
        # f.epix=rotate(f.epix, angle=self.angle, axes=[1,2])
        for fil in self.filters:
            analysis.filter_shots(f,fil['FilterType'],fil['FilterKey'],fil['FilterThreshold'])                                           
        analysis.reduce_detector_spatial(f,'epix', rois=self.rois, adu_cutoff=self.adu_cutoff, reduction_axis=1)
        keys_to_save=['start_index','end_index','run_file','run_number','verbose','status','status_datetime','epix_ROI_1']
        # f.purge_all_keys(keys_to_save)
        analysis.make_energy_axis(f,f.epix_ROI_1.shape[1],d=self.crystal_d_space,R=self.crystal_radius,A=self.crystal_detector_distance)
        return f
  
    def primary_analysis(self,run,experiment,verbose=False,start_index=None,end_index=None):
        if end_index==None:
            end_index=self.end_index
        if start_index==None:
            try:
                start_index=self.start_index
            except AttributeError:
                start_index=0
        self.end_index=end_index
        self.start_index=start_index
        f=spectroscopy_run(experiment,run,verbose=verbose,start_index=start_index,end_index=end_index)
        f.get_run_shot_properties()
        f.load_run_keys(self.keys,self.friendly_names)
        f.load_run_key_delayed(self.key_epix,self.friendly_name_epix)
        analysis=XESAnalysis()
        analysis.pixels_to_patch=self.pixels_to_patch
        analysis.filter_detector_adu(f,'epix',adu_threshold=self.adu_cutoff)
        analysis.patch_pixels(f,'epix',axis=1)
        f.epix=rotate(f.epix, angle=self.angle, axes=[1,2])
        for fil in self.filters:
            analysis.filter_shots(f,fil['FilterType'],fil['FilterKey'],fil['FilterThreshold'])                                                                  
        analysis.union_shots(f,'epix',['simultaneous','laser'])
        analysis.separate_shots(f,'epix',['xray','laser'])
        self.bins=np.linspace(self.mintime,self.maxtime,self.numpoints)
        analysis.time_binning(f,self.bins)
        analysis.union_shots(f,'timing_bin_indices',['simultaneous','laser'])
        analysis.separate_shots(f,'timing_bin_indices',['xray','laser'])
        analysis.reduce_detector_temporal(f,'epix_simultaneous_laser','timing_bin_indices_simultaneous_laser',average=False)
        analysis.reduce_detector_temporal(f,'epix_xray_not_laser','timing_bin_indices_xray_not_laser',average=False)
        analysis.reduce_detector_spatial(f,'epix_simultaneous_laser_time_binned', rois=self.rois,adu_cutoff=self.adu_cutoff)
        analysis.reduce_detector_spatial(f,'epix_xray_not_laser_time_binned', rois=self.rois,adu_cutoff=self.adu_cutoff)
        analysis.make_energy_axis(f,f.epix_xray_not_laser_time_binned_ROI_1.shape[1],d=self.crystal_d_space,R=self.crystal_radius,A=self.crystal_detector_distance)
        keys_to_save=['start_index','end_index','run_file','run_number','verbose','status','status_datetime','epix_xray_not_laser_time_binned_ROI_1','epix_simultaneous_laser_time_binned_ROI_1']
        f.purge_all_keys(keys_to_save)
        analysis.make_energy_axis(f,f.epix_xray_not_laser_time_binned_ROI_1.shape[1],d=self.crystal_d_space,R=self.crystal_radius,A=self.crystal_detector_distance)
        return f
    def primary_analysis_range(self, experiment, run, shot_ranges, verbose=False):

        start, end = shot_ranges
        return self.primary_analysis(run=run,experiment=experiment, start_index=start, end_index=end, verbose=verbose)
            
        
            

        
    

class XASBatchAnalysis(BatchAnalysis):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mintime=-2.0
        self.maxtime=10.0
        self.numpoints=240
        self.minccm=7.105
        self.maxccm=7.135
        self.numpoints_ccm=90
        self.time_bins=np.linspace(self.mintime,self.maxtime,self.numpoints)
        self.filters=[]
    def primary_analysis(self,experiment,run,verbose=False):
        f=spectroscopy_run(experiment,run,verbose=verbose)
        f.get_run_shot_properties()
        
        f.load_run_keys(self.keys,self.friendly_names)
        analysis=XASAnalysis()
        try:
            ccm_val = getattr(f, 'ccm_E_setpoint')
            elist = np.unique(ccm_val)
        except KeyError as e:
            self.update_status('Key does not exist: %s' % e.args[0])
            elist = np.linspace(self.minccm,self.maxccm,self.numpoints_ccm)
        analysis.make_ccm_axis(f,elist)
        for fil in self.filters:
            analysis.filter_shots(f,fil['FilterType'],fil['FilterKey'],fil['FilterThreshold']) 
        analysis.union_shots(f,'epix',['simultaneous','laser'])
        analysis.separate_shots(f,'epix',['xray','laser'])
        analysis.union_shots(f,'ipm',['simultaneous','laser'])
        analysis.separate_shots(f,'ipm',['xray','laser'])
        analysis.union_shots(f,'ccm',['simultaneous','laser'])
        analysis.separate_shots(f,'ccm',['xray','laser'])
        self.time_bins=np.linspace(self.mintime,self.maxtime,self.numpoints)
        analysis.time_binning(f,self.time_bins)
        analysis.ccm_binning(f,'ccm_bins','ccm')
        analysis.union_shots(f,'timing_bin_indices',['simultaneous','laser'])
        analysis.separate_shots(f,'timing_bin_indices',['xray','laser'])
        analysis.union_shots(f,'ccm_bin_indices',['simultaneous','laser'])
        analysis.separate_shots(f,'ccm_bin_indices',['xray','laser'])
        analysis.reduce_detector_ccm_temporal(f,'epix_simultaneous_laser','timing_bin_indices_simultaneous_laser','ccm_bin_indices_simultaneous_laser',average=False)
        analysis.reduce_detector_ccm_temporal(f,'epix_xray_not_laser','timing_bin_indices_xray_not_laser','ccm_bin_indices_xray_not_laser',average=False)
        analysis.reduce_detector_ccm_temporal(f,'ipm_simultaneous_laser','timing_bin_indices_simultaneous_laser','ccm_bin_indices_simultaneous_laser',average=False)
        analysis.reduce_detector_ccm_temporal(f,'ipm_xray_not_laser','timing_bin_indices_xray_not_laser','ccm_bin_indices_xray_not_laser',average=False)
        return f

class XASBatchAnalysis_1D_ccm(BatchAnalysis):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.minccm=7.105
        self.maxccm=7.135
        self.numpoints_ccm=90
        self.filters=[]
    def primary_analysis(self,experiment,run,verbose=False):
        f=spectroscopy_run(experiment,run,verbose=verbose)
        f.get_run_shot_properties()
        
        f.load_run_keys(self.keys,self.friendly_names)
        analysis=XASAnalysis()
        try:
            ccm_val = getattr(f, 'ccm_E_setpoint')
            elist = np.unique(ccm_val)
        except KeyError as e:
            self.update_status('Key does not exist: %s' % e.args[0])
            elist = np.linspace(self.minccm,self.maxccm,self.numpoints_ccm)
        analysis.make_ccm_axis(f,elist)
        for fil in self.filters:
            analysis.filter_shots(f,fil['FilterType'],fil['FilterKey'],fil['FilterThreshold']) 
        analysis.union_shots(f,'epix',['simultaneous','laser'])
        analysis.separate_shots(f,'epix',['xray','laser'])
        analysis.union_shots(f,'ipm',['simultaneous','laser'])
        analysis.separate_shots(f,'ipm',['xray','laser'])
        analysis.union_shots(f,'ccm',['simultaneous','laser'])
        analysis.separate_shots(f,'ccm',['xray','laser'])
#         self.time_bins=np.linspace(self.mintime,self.maxtime,self.numpoints)
#         analysis.time_binning(f,self.time_bins)
        analysis.ccm_binning(f,'ccm_bins','ccm')
#         analysis.union_shots(f,'timing_bin_indices',['simultaneous','laser'])
#         analysis.separate_shots(f,'timing_bin_indices',['xray','laser'])
        analysis.union_shots(f,'ccm_bin_indices',['simultaneous','laser'])
        analysis.separate_shots(f,'ccm_bin_indices',['xray','laser'])
        analysis.reduce_detector_ccm(f,'epix_simultaneous_laser','ccm_bin_indices_simultaneous_laser',average=False)
        analysis.reduce_detector_ccm(f,'epix_xray_not_laser','ccm_bin_indices_xray_not_laser',average=False)
        analysis.reduce_detector_ccm(f,'ipm_simultaneous_laser','ccm_bin_indices_simultaneous_laser',average=False)
        analysis.reduce_detector_ccm(f,'ipm_xray_not_laser','ccm_bin_indices_xray_not_laser',average=False)
        return f

class XASBatchAnalysis_1D_time(BatchAnalysis):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mintime=-2.0
        self.maxtime=10.0
        self.numpoints=240
        self.minccm=7.105
        self.maxccm=7.135
        self.numpoints_ccm=90
        self.time_bins=np.linspace(self.mintime,self.maxtime,self.numpoints)
        self.filters=[]
    def primary_analysis(self,experiment,run,verbose=False):
        f=spectroscopy_run(experiment,run,verbose=verbose)
        f.get_run_shot_properties()
        
        f.load_run_keys(self.keys,self.friendly_names)
        analysis=XASAnalysis()
#         try:
#             ccm_val = getattr(f, 'ccm_E_setpoint')
#             elist = np.unique(ccm_val)
#         except KeyError as e:
#             self.update_status('Key does not exist: %s' % e.args[0])
#             elist = np.linspace(self.minccm,self.maxccm,self.numpoints_ccm)
#         analysis.make_ccm_axis(f,elist)
        for fil in self.filters:
            analysis.filter_shots(f,fil['FilterType'],fil['FilterKey'],fil['FilterThreshold']) 
        analysis.union_shots(f,'epix',['simultaneous','laser'])
        analysis.separate_shots(f,'epix',['xray','laser'])
        analysis.union_shots(f,'ipm',['simultaneous','laser'])
        analysis.separate_shots(f,'ipm',['xray','laser'])
#         analysis.union_shots(f,'ccm',['simultaneous','laser'])
#         analysis.separate_shots(f,'ccm',['xray','laser'])
        self.time_bins=np.linspace(self.mintime,self.maxtime,self.numpoints)
        analysis.time_binning(f,self.time_bins)
#         analysis.ccm_binning(f,'ccm_bins','ccm')
        analysis.union_shots(f,'timing_bin_indices',['simultaneous','laser'])
        analysis.separate_shots(f,'timing_bin_indices',['xray','laser'])
#         analysis.union_shots(f,'ccm_bin_indices',['simultaneous','laser'])
#         analysis.separate_shots(f,'ccm_bin_indices',['xray','laser'])
        analysis.reduce_detector_temporal(f,'epix_simultaneous_laser','timing_bin_indices_simultaneous_laser',average=False)
        analysis.reduce_detector_temporal(f,'epix_xray_not_laser','timing_bin_indices_xray_not_laser',average=False)
        analysis.reduce_detector_temporal(f,'ipm_simultaneous_laser','timing_bin_indices_simultaneous_laser',average=False)
        analysis.reduce_detector_temporal(f,'ipm_xray_not_laser','timing_bin_indices_xray_not_laser',average=False)
        return f

class ScanAnalysis_1D(BatchAnalysis):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        pass
    def primary_analysis(self,experiment,run,verbose=False):
        f=spectroscopy_run(experiment,run=run,verbose=True)
        analysis=XASAnalysis()
        f.get_run_shot_properties()
        f.load_run_keys(self.keys,self.friendly_names)
        analysis.bin_uniques(f,'scan')
        analysis.union_shots(f,'epix',['simultaneous','laser'])
        analysis.separate_shots(f,'epix',['xray','laser'])
        analysis.union_shots(f,'ipm',['simultaneous','laser'])
        analysis.separate_shots(f,'ipm',['xray','laser'])
        analysis.union_shots(f,'scan',['simultaneous','laser'])
        analysis.separate_shots(f,'scan',['xray','laser'])
        analysis.union_shots(f,'scanvar_indices',['simultaneous','laser'])
        analysis.separate_shots(f,'scanvar_indices',['xray','laser'])
        analysis.reduce_detector_ccm(f,'epix_simultaneous_laser','scanvar_indices_simultaneous_laser',average=False)
        analysis.reduce_detector_ccm(f,'epix_xray_not_laser','scanvar_indices_xray_not_laser',average=False)
        analysis.reduce_detector_ccm(f,'ipm_simultaneous_laser','scanvar_indices_simultaneous_laser',average=False)
        analysis.reduce_detector_ccm(f,'ipm_xray_not_laser','scanvar_indices_xray_not_laser',average=False)
        return f
