__all__ = ['MDReg', 'default_bspline']

import time, os, copy
import multiprocessing

from tqdm import tqdm
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.animation as animation
#import gc

import itk
from dipy.align.imwarp import SymmetricDiffeomorphicRegistration
from dipy.align.metrics import CCMetric, EMMetric, SSDMetric
from skimage.registration import optical_flow_tvl1
from skimage.transform import warp
from skimage.measure import block_reduce

default_path = os.path.dirname(__file__)
try: 
    num_workers = int(len(os.sched_getaffinity(0)))
except: 
    num_workers = int(os.cpu_count())

class MDReg:

    def __init__(self):

        # input DEFAULT
        self.array = None
        self.coreg_mask = None
        self.signal_parameters = {}
        self.pixel_spacing = 1.0
        #self.signal_model = constant.main
        self.log = False
        self.parallel = True
        self.package = 'elastix'
        self.downsample = 2

        # Rename this as one field self.coreg_parameters
        self.elastix = default_bspline('2')
        self.dipy = {'transform': 'Symmetric Diffeomorphic', 'metric':"Cross-Correlation"}
        self.skimage = {'attachment':1}

        # mdr optimization
        self.max_iterations = 5
        self.precision = 1.0

        # output
        self.coreg = None
        self.model_fit = None
        self.deformation = None
        self.pars = None
        self.iter = None
        self.export_path = os.path.join(default_path, 'results')
        self.export_unregistered = False

        # status
        self.status = None
        self.pinned_message = ''
        self.message = ''
        self.iteration = 1

    @property
    def _npdt(self): 
        """
        (nr of pixels, nr of dimensions, nr of time points)
        """
        shape = self.array.shape
        return np.prod(shape[:-1]), len(shape)-1, shape[-1]

    def set_array(self, array):
        self.array = array
        self.coreg = array
        n = self._npdt
        self.coreg = np.reshape(self.coreg, (n[0],n[2]))
        if n[1] == 3:
            self.elastix = default_bspline('3')
        if self.package == 'skimage':
            rc, cc = np.meshgrid( 
                np.arange(array.shape[0]), 
                np.arange(array.shape[1]),
                indexing='ij')
            self.skimage['row_coords'] = rc
            self.skimage['col_coords'] = cc
    
    def set_mask(self, mask_array):
        self.coreg_mask = mask_array
        n = self._npdt
        self.coreg_mask = np.reshape(self.coreg_mask, (n[0],n[2]))

    def read_elastix(self, file):
        self.elastix.AddParameterFile(file)
    
    def set_elastix(self, **kwargs):
        for tag, value in kwargs.items():
            self.elastix.SetParameter(tag, str(value))       

    def fit(self):
        n = self._npdt
        self.coreg = copy.deepcopy(self.array)
        self.coreg = np.reshape(self.coreg, (n[0],n[2]))
        self.deformation = np.zeros(n)
        start = time.time()
        improvement = []
        converged = False
        self.iteration = 1
        while not converged: 
            startit = time.time()
            self.fit_signal() 
            if self.export_unregistered:
                if self.iteration == 1: 
                    self.export_fit(name='_unregistered')
            deformation = self.fit_deformation()
            #reshape for maxnorm
            deformation  = np.reshape(deformation,[n[0],n[1],n[2]])
            improvement.append(_maxnorm(self.deformation-deformation))
            self.deformation = deformation
            converged = improvement[-1] <= self.precision 
            if self.iteration == self.max_iterations: 
                converged=True
            calctime = (time.time()-startit)/60
            print('Finished MDR iteration ' + str(self.iteration) + ' after ' + str(calctime) + ' min') 
            print('Improvement after MDR iteration ' + str(self.iteration) + ': ' + str(improvement[-1]) + ' pixels')  
            #del deformation
            #gc.collect()
            self.iteration += 1 

        self.fit_signal()
        shape = self.array.shape
        self.coreg = np.reshape(self.coreg, shape)
        nd = len(shape)-1
        self.deformation = np.reshape(self.deformation, shape[:-1]+(nd,)+(shape[-1],))
        self.iter = pd.DataFrame({'Maximum deformation': improvement}) 

        print('Total calculation time: ' + str((time.time()-start)/60) + ' min')
        #gc.collect()

    def fit_signal(self):

        msg = self.pinned_message + ' fitting signal model (iteration ' + str(self.iteration) + ')'
        print(msg)
        if self.status is not None:
            self.status.message(msg)
        start = time.time()
        fit, pars = self.signal_model(self.coreg, **self.signal_parameters)
        shape = self.array.shape
        self.model_fit = np.reshape(fit, shape) 
        self.pars = np.reshape(pars, shape[:-1] + (pars.shape[-1],))
       
        print('Model fitting time: ' + str((time.time()-start)/60) + ' min')


    def fit_deformation(self):

        msg = self.pinned_message + ', fitting deformation field (iteration ' + str(self.iteration) + ')'
        if self.status is not None:
            self.status.message(msg)
        start = time.time()
        nt = self._npdt[-1]

        deformation = np.empty(self._npdt)
        
        # reshape the deformation field 
        if self._npdt[1] == 2: #2D
            deformation = np.reshape(deformation,(self.array.shape[0], self.array.shape[1], 2, self.array.shape[2])) 
        else: #3D
            deformation = np.reshape(deformation,(self.array.shape[0], self.array.shape[1], self.array.shape[2], 3, self.array.shape[3])) 

        # If mask isn't same shape as images, then don't use it
        if isinstance(self.coreg_mask, np.ndarray):
            if np.shape(self.coreg_mask) != self.array.shape: 
                mask = None
                print("using mask shape: If mask isn't same shape as images, then don't use it")
            else: 
                mask = self.coreg_mask
        else: 
            mask = None

        if self.parallel == False:

            for t in tqdm(range(nt), desc=msg): 
                if self.status is not None:
                    self.status.progress(t, nt)

                if mask is not None:
                    mask_t = mask[...,t]
                else: 
                    mask_t = None
                
                if self.package=='elastix':
                    try:
                        self.coreg[:,t], deformation[...,t] = _coregister_elastix(
                            self.array[...,t], 
                            self.model_fit[...,t], 
                            self.elastix, 
                            self.pixel_spacing, 
                            self.log, 
                            mask_t,
                            self.downsample,
                        )
                    except:
                        # An error sometimes happpens when too many samples are outside the image buffer
                        # Pass over quietly in that case
                        pass
                elif self.package=='dipy':
                    self.coreg[:,t], deformation[...,t] = _coregister_dipy(
                        self.array[...,t], 
                        self.model_fit[...,t], 
                        self.dipy,  
                        self.pixel_spacing, 
                        self.log, 
                        mask_t,
                    )
                elif self.package=='skimage':
                    self.coreg[:,t], deformation[...,t] = _coregister_skimage(
                        self.array[...,t], 
                        self.model_fit[...,t], 
                        self.skimage,  
                        self.pixel_spacing, 
                        self.log, 
                        mask_t,
                    )

        if self.parallel == True:
            pool = multiprocessing.Pool(processes=num_workers)
            if self.package=='elastix':
                dict_param = _elastix2dict(self.elastix)
                if mask is None:
                    args = [(self.array[...,t], self.model_fit[...,t], dict_param, self.pixel_spacing, self.log, mask, self.downsample) for t in range(nt)]
                else:
                    args = [(self.array[...,t], self.model_fit[...,t], dict_param, self.pixel_spacing, self.log, mask[...,t], self.downsample) for t in range(nt)]
                results = list(tqdm(pool.map(_coregister_elastix_parallel, args), total=nt, desc=msg))
            elif self.package=='dipy':
                if mask is None:
                    args = [(self.array[...,t], self.model_fit[...,t], self.dipy, self.pixel_spacing, self.log, mask) for t in range(nt)]
                else:
                    args = [(self.array[...,t], self.model_fit[...,t], self.dipy, self.pixel_spacing, self.log, mask[...,t]) for t in range(nt)]
                results = list(tqdm(pool.imap(_coregister_dipy_parallel, args), total=nt, desc=msg))
            elif self.package=='skimage':
                if mask is None:
                    args = [(self.array[...,t], self.model_fit[...,t], self.skimage, self.pixel_spacing, self.log, mask) for t in range(nt)]
                else:
                    args = [(self.array[...,t], self.model_fit[...,t], self.skimage, self.pixel_spacing, self.log, mask[...,t]) for t in range(nt)]
                results = list(tqdm(pool.imap(_coregister_skimage_parallel, args), total=nt, desc=msg))

            # Good practice to close and join when the pool is no longer needed
            # https://stackoverflow.com/questions/38271547/when-should-we-call-multiprocessing-pool-join
            pool.close()
            pool.join()

            for t in range(nt):
                self.coreg[:,t] = results[t][0]
                deformation[...,t] = results[t][1]

            #del args #??
            #gc.collect()

        print('Coregistration time: ' + str((time.time()-start)/60) +' min')

        return deformation

    def export(self):

        self.export_data()
        self.export_fit()
        self.export_registered()

    def export_data(self):

        print('Exporting data..')
        path = self.export_path 
        if not os.path.exists(path): os.mkdir(path)
        _export_animation(self.array, path, 'images')

    def export_fit(self, pars, bounds, name=''):

        print('Exporting fit..' + name)
        path = self.export_path 
    
        if not os.path.exists(path): os.mkdir(path)
        lower, upper = bounds
        for i in range(len(pars)):
            _export_imgs(self.pars[...,i], path, pars[i] + name, bounds=[lower[i],upper[i]])
        _export_animation(self.model_fit, path, 'modelfit' + name)
        

    def export_registered(self):

        print('Exporting registration..')
        path = self.export_path 
        if not os.path.exists(path): os.mkdir(path)
        _export_animation(self.coreg, path, 'coregistered')
        defx = np.squeeze(self.deformation[...,0,:])
        defy = np.squeeze(self.deformation[...,1,:])
        _export_animation(defx, path, 'deformation_field_x')
        _export_animation(defy, path, 'deformation_field_y')
        if self._npdt[1] == 3: #3D
            defz = np.squeeze(self.deformation[...,2,:])
            _export_animation(defz, path, 'deformation_field_z')
            _export_animation(np.sqrt(defx**2 + defy**2 + defz**2), path, 'deformation_field')
        else: #2D
            _export_animation(np.sqrt(defx**2 + defy**2), path, 'deformation_field')
        self.iter.to_csv(os.path.join(path, 'largest_deformations.csv'))




def _export_animation(array, path, filename):

    file = os.path.join(path, filename + '.gif')
    array[np.isnan(array)] = 0
    shape = np.shape(array)

    if len(shape)==4: ##save 3D data
        fig = plt.figure()
        file_3D_save = os.path.join(path, filename)
        for k in range(shape[2]): 
            im = plt.imshow(np.squeeze(array[:,:,k,0]).T, animated=True) 
            def updatefig(i):
                im.set_array(np.squeeze(array[:,:,k,i]).T) # export each slice all dynamics
            anim = animation.FuncAnimation(fig, updatefig, interval=50, frames=array.shape[3])
            anim.save(file_3D_save + '_' + str(k) + ".gif")

    else: # save 2D data   
        fig = plt.figure()
        im = plt.imshow(np.squeeze(array[:,:,0]).T, animated=True) 
        def updatefig(i):
            im.set_array(np.squeeze(array[:,:,i]).T) 
        anim = animation.FuncAnimation(fig, updatefig, interval=50, frames=array.shape[2])
        anim.save(file) 


def _export_imgs(array, path, filename, bounds=[-np.inf, np.inf]):

    file = os.path.join(path, filename + '.png')
    array[np.isnan(array)] = 0 
    array[np.isinf(array)] = 0
    array = np.clip(array, bounds[0], bounds[1])
    shape_arr = np.shape(array)

    if len(shape_arr) == 2: #2D data save
        plt.imshow((array).T)
        plt.clim(np.amin(array), np.amax(array))
        cBar = plt.colorbar()
        cBar.minorticks_on()
        plt.savefig(fname=file)
        plt.close()
    else: #3D data save
        file_3D = os.path.join(path, filename)
        for i in range(shape_arr[2]):
            plt.imshow((array[:,:,i]).T)
            plt.clim(np.amin(array), np.amax(array))
            cBar = plt.colorbar()
            cBar.minorticks_on()
            plt.savefig(fname=[file_3D + '_' + str(i) + ".png"])
            plt.close()
   

def _maxnorm(d):
    """This function calculates diagnostics from the registration process.

    It takes as input the original deformation field and the new deformation field
    and returns the maximum deformation per pixel (in mm).
    The maximum deformation per pixel is calculated as 
    the euclidean distance of difference between the old and new deformation field. 
    """
    shape_deformation = np.shape(d) 
    if (shape_deformation[1] == 3): #3D
       d = d[:,0,:]**2 + d[:,1,:]**2 + d[:,2,:]**2
    else: #2D
       d = d[:,0,:]**2 + d[:,1,:]**2

    return np.nanmax(np.sqrt(d))



def _coregister_skimage_parallel(args):
    moving, fixed, parameters, spacing, log, mask = args
    return _coregister_skimage(moving, fixed, parameters, spacing, log, mask)

def _coregister_skimage(moving, fixed, parameters, spacing, log, mask):
    row_coords = parameters['row_coords']
    col_coords = parameters['col_coords']
    v, u = optical_flow_tvl1(fixed, moving, attachment=parameters['attachment'])
    new_coords = np.array([row_coords + v, col_coords + u])
    warped_moving = warp(moving, new_coords, mode='edge')
    deformation_field = np.stack([v, u], axis=-1)
    return warped_moving.flatten(), deformation_field


def _coregister_dipy_parallel(args):
    moving, fixed, parameters, spacing, log, mask = args
    return _coregister_dipy(moving, fixed, parameters, spacing, log, mask)

def _coregister_dipy(moving, fixed, parameters, spacing, log, mask):
    
    dim = fixed.ndim

    # 3D registration does not seem to work with smaller slabs
    # Exclude this case
    if dim == 3:
        if fixed.shape[-1] < 6:
            msg = 'The 3D volume does not have enough slices for 3D registration. \n'
            msg += 'Try 2D registration instead.'
            raise ValueError(msg)
        
    # Define the metric
    metric = parameters['metric'] # default = metric="Cross-Correlation"
    if metric == "Cross-Correlation":
        sigma_diff = 3.0    # Gaussian Kernel
        radius = 4          # Window for local CC
        metric = CCMetric(dim, sigma_diff, radius)
    elif metric == 'Expectation-Maximization':
        metric = EMMetric(dim, smooth=1.0)
    elif metric == 'Sum of Squared Differences':
        metric = SSDMetric(dim, smooth=4.0)
    else:
        msg = 'The metric ' + metric + ' is currently not implemented.'
        raise ValueError(msg) 

    # Define the deformation model
    transformation = parameters['transform'] # default='Symmetric Diffeomorphic'
    if transformation == 'Symmetric Diffeomorphic':
        level_iters = [100, 50, 25]
        sdr = SymmetricDiffeomorphicRegistration(metric, level_iters, inv_iter=50)
    else:
        msg = 'The transform ' + transformation + ' is currently not implemented.'
        raise ValueError(msg) 

    # Perform the optimization, return a DiffeomorphicMap object
    mapping = sdr.optimize(fixed, moving)

    # Get forward deformation field
    deformation_field = mapping.get_forward_field()

    # Warp the moving image
    warped_moving = mapping.transform(moving, 'linear')

    return warped_moving.flatten(), deformation_field



def __coregister_elastix_parallel(args):
    """
    Coregister two arrays and return coregistered + deformation field 
    """
    source, target, elastix_model_parameters, spacing, log, mask = args
    elastix_model_parameters = _dict2elastix(elastix_model_parameters)
    return _coregister_elastix(source, target, elastix_model_parameters, spacing, log, mask)


def __coregister_elastix(source, target, elastix_model_parameters, spacing, log, mask):
    """
    Coregister two arrays and return coregistered + deformation field 
    """

    shape_source = np.shape(source)

    # Coregister source to target
    source = itk.GetImageFromArray(np.array(source, np.float32)) 
    target = itk.GetImageFromArray(np.array(target, np.float32))
    source.SetSpacing(spacing)
    target.SetSpacing(spacing)
    coregistered, result_transform_parameters = itk.elastix_registration_method(
        target, source, parameter_object=elastix_model_parameters, log_to_console=False)
    coregistered = itk.GetArrayFromImage(coregistered).flatten()

    # Get deformation field
    deformation_field = itk.transformix_deformation_field(
        target, 
        result_transform_parameters, 
        log_to_console=log)
    deformation_field = itk.GetArrayFromImage(deformation_field).flatten()
    deformation_field = np.reshape(deformation_field, shape_source + (len(shape_source), ))

    return coregistered, deformation_field


def _coregister_elastix_parallel(args):
    """
    Coregister two arrays and return coregistered + deformation field 
    """
    source, target, elastix_model_parameters, spacing, log, mask, downsample = args
    elastix_model_parameters = _dict2elastix(elastix_model_parameters)
    return _coregister_elastix(source, target, elastix_model_parameters, spacing, log, mask, downsample)


def _coregister_elastix(source_large, target_large, elastix_model_parameters, spacing_large, log, mask, downsample:int=1):
    """
    Coregister two arrays and return coregistered + deformation field 
    """

    # Downsample source and target
    # The origin of an image is the center of the voxel in the lower left corner
    # The origin of the large image is (0,0).
    # The original of the small image is therefore: 
    #   spacing_large/2 + (spacing_small/2 - spacing_large)
    #   = (spacing_small - spacing_large)/2
    target_small = block_reduce(target_large, block_size=downsample, func=np.mean)
    source_small = block_reduce(source_large, block_size=downsample, func=np.mean)
    spacing_small = [spacing_large[1]*downsample, spacing_large[0]*downsample]
    origin_large = [0,0]
    origin_small = [(spacing_small[1] - spacing_large[1])/2, (spacing_small[0] - spacing_large[0]) / 2]

    # Coregister downsampled source to target
    source_small = itk.GetImageFromArray(np.array(source_small, np.float32)) 
    target_small = itk.GetImageFromArray(np.array(target_small, np.float32))
    source_small.SetSpacing(spacing_small)
    target_small.SetSpacing(spacing_small)
    source_small.SetOrigin(origin_small)
    target_small.SetOrigin(origin_small)
    coreg_small, result_transform_parameters = itk.elastix_registration_method(
        target_small, source_small,
        parameter_object=elastix_model_parameters, 
        log_to_console=log)
    
    # Get coregistered image at original size
    result_transform_parameters.SetParameter(0, "Size", [str(source_large.shape[1]), str(source_large.shape[0])])
    result_transform_parameters.SetParameter(0, "Spacing", [str(spacing_large[1]), str(spacing_large[0])])
    source_large = itk.GetImageFromArray(np.array(source_large, np.float32))
    source_large.SetSpacing(spacing_large)
    source_large.SetOrigin(origin_large)
    coreg_large = itk.transformix_filter(
        source_large,
        result_transform_parameters,
        log_to_console=log)
    coreg_large = itk.GetArrayFromImage(coreg_large).flatten()
    
    # Get deformation field at original size
    target_large = itk.GetImageFromArray(np.array(target_large, np.float32))
    target_large.SetSpacing(spacing_large)
    target_large.SetOrigin(origin_large)
    deformation_field = itk.transformix_deformation_field(
        target_large, 
        result_transform_parameters, 
        log_to_console=log)
    deformation_field = itk.GetArrayFromImage(deformation_field).flatten()
    #deformation_field = np.reshape(deformation_field, (target_large.shape[0], target_large.shape[1], 2)) 
    deformation_field = np.reshape(deformation_field, target_large.shape + (len(target_large.shape), ))
    return coreg_large, deformation_field


def _elastix2dict(elastix_model_parameters):
    """
    Hack to allow parallel processing
    """
    list_dictionaries_parameters = []
    for index in range(elastix_model_parameters.GetNumberOfParameterMaps()):
        parameter_map = elastix_model_parameters.GetParameterMap(index)
        one_parameter_map_dict = {}
        for i in parameter_map:
            one_parameter_map_dict[i] = parameter_map[i]
        list_dictionaries_parameters.append(one_parameter_map_dict)
    return list_dictionaries_parameters


def _dict2elastix(list_dictionaries_parameters):
    """
    Hack to allow parallel processing
    """
    elastix_model_parameters = itk.ParameterObject.New() # slow!!!
    for one_map in list_dictionaries_parameters:
        elastix_model_parameters.AddParameterMap(one_map)
    return elastix_model_parameters


def default_bspline(d):
    param_obj = itk.ParameterObject.New()
    parameter_map_bspline = param_obj.GetDefaultParameterMap('bspline')
    ## add parameter map file to the parameter object: required in itk-elastix
    param_obj.AddParameterMap(parameter_map_bspline) 
    #OPTIONAL: Write the default parameter file to output file
    # param_obj.WriteParameterFile(parameter_map_bspline, "bspline.default.txt")
    # *********************
    # * ImageTypes
    # *********************
    param_obj.SetParameter("FixedInternalImagePixelType", "float")
    param_obj.SetParameter("MovingInternalImagePixelType", "float")
    ## selection based on 3D or 2D image data: newest elastix version does not require input image dimension
    param_obj.SetParameter("FixedImageDimension", d) 
    param_obj.SetParameter("MovingImageDimension", d) 
    param_obj.SetParameter("UseDirectionCosines", "true")
    # *********************
    # * Components
    # *********************
    param_obj.SetParameter("Registration", "MultiResolutionRegistration")
    # Image intensities are sampled using an ImageSampler, Interpolator and ResampleInterpolator.
    # Image sampler is responsible for selecting points in the image to sample. 
    # The RandomCoordinate simply selects random positions.
    param_obj.SetParameter("ImageSampler", "RandomCoordinate")
    # Interpolator is responsible for interpolating off-grid positions during optimization. 
    # The BSplineInterpolator with BSplineInterpolationOrder = 1 used here is very fast and uses very little memory
    param_obj.SetParameter("Interpolator", "BSplineInterpolator")
    # ResampleInterpolator here chosen to be FinalBSplineInterpolator with FinalBSplineInterpolationOrder = 1
    # is used to resample the result image from the moving image once the final transformation has been found.
    # This is a one-time step so the additional computational complexity is worth the trade-off for higher image quality.
    param_obj.SetParameter("ResampleInterpolator", "FinalBSplineInterpolator")
    param_obj.SetParameter("Resampler", "DefaultResampler")
    # Order of B-Spline interpolation used during registration/optimisation.
    # It may improve accuracy if you set this to 3. Never use 0.
    # An order of 1 gives linear interpolation. This is in most 
    # applications a good choice.
    param_obj.SetParameter("BSplineInterpolationOrder", "1")
    # Order of B-Spline interpolation used for applying the final
    # deformation.
    # 3 gives good accuracy; recommended in most cases.
    # 1 gives worse accuracy (linear interpolation)
    # 0 gives worst accuracy, but is appropriate for binary images
    # (masks, segmentations); equivalent to nearest neighbor interpolation.
    param_obj.SetParameter("FinalBSplineInterpolationOrder", "3")
    # Pyramids found in Elastix:
    # 1)	Smoothing -> Smoothing: YES, Downsampling: NO
    # 2)	Recursive -> Smoothing: YES, Downsampling: YES
    #      If Recursive is chosen and only # of resolutions is given 
    #      then downsamlping by a factor of 2 (default)
    # 3)	Shrinking -> Smoothing: NO, Downsampling: YES
    param_obj.SetParameter("FixedImagePyramid", "FixedSmoothingImagePyramid")
    param_obj.SetParameter("MovingImagePyramid", "MovingSmoothingImagePyramid")
    param_obj.SetParameter("Optimizer", "AdaptiveStochasticGradientDescent")
    # Whether transforms are combined by composition or by addition.
    # In generally, Compose is the best option in most cases.
    # It does not influence the results very much.
    param_obj.SetParameter("HowToCombineTransforms", "Compose")
    param_obj.SetParameter("Transform", "BSplineTransform")
    # Metric
    param_obj.SetParameter("Metric", "AdvancedMeanSquares")
    # Number of grey level bins in each resolution level,
    # for the mutual information. 16 or 32 usually works fine.
    # You could also employ a hierarchical strategy:
    #(NumberOfHistogramBins 16 32 64)
    param_obj.SetParameter("NumberOfHistogramBins", "32")
    # *********************
    # * Transformation
    # *********************
    # The control point spacing of the bspline transformation in 
    # the finest resolution level. Can be specified for each 
    # dimension differently. Unit: mm.
    # The lower this value, the more flexible the deformation.
    # Low values may improve the accuracy, but may also cause
    # unrealistic deformations.
    # By default the grid spacing is halved after every resolution,
    # such that the final grid spacing is obtained in the last 
    # resolution level.
    # The grid spacing here is specified in voxel units.
    #(FinalGridSpacingInPhysicalUnits 10.0 10.0)
    #(FinalGridSpacingInVoxels 8)
    #param_obj.SetParameter("FinalGridSpacingInPhysicalUnits", ["50.0", "50.0"])
    param_obj.SetParameter("FinalGridSpacingInPhysicalUnits", "50.0")
    # *********************
    # * Optimizer settings
    # *********************
    # The number of resolutions. 1 Is only enough if the expected
    # deformations are small. 3 or 4 mostly works fine. For large
    # images and large deformations, 5 or 6 may even be useful.
    param_obj.SetParameter("NumberOfResolutions", "4")
    param_obj.SetParameter("AutomaticParameterEstimation", "true")
    param_obj.SetParameter("ASGDParameterEstimationMethod", "Original")
    param_obj.SetParameter("MaximumNumberOfIterations", "500")
    # The step size of the optimizer, in mm. By default the voxel size is used.
    # which usually works well. In case of unusual high-resolution images
    # (eg histology) it is necessary to increase this value a bit, to the size
    # of the "smallest visible structure" in the image:
    param_obj.SetParameter("MaximumStepLength", "1.0") 
    # *********************
    # * Pyramid settings
    # *********************
    # The downsampling/blurring factors for the image pyramids.
    # By default, the images are downsampled by a factor of 2
    # compared to the next resolution.
    #param_obj.SetParameter("ImagePyramidSchedule", "8 8  4 4  2 2  1 1")
    # *********************
    # * Sampler parameters
    # *********************
    # Number of spatial samples used to compute the mutual
    # information (and its derivative) in each iteration.
    # With an AdaptiveStochasticGradientDescent optimizer,
    # in combination with the two options below, around 2000
    # samples may already suffice.
    param_obj.SetParameter("NumberOfSpatialSamples", "2048")
    # Refresh these spatial samples in every iteration, and select
    # them randomly. See the manual for information on other sampling
    # strategies.
    param_obj.SetParameter("NewSamplesEveryIteration", "true")
    param_obj.SetParameter("CheckNumberOfSamples", "true")
    # *********************
    # * Mask settings
    # *********************
    # If you use a mask, this option is important. 
    # If the mask serves as region of interest, set it to false.
    # If the mask indicates which pixels are valid, then set it to true.
    # If you do not use a mask, the option doesn't matter.
    param_obj.SetParameter("ErodeMask", "false")
    param_obj.SetParameter("ErodeFixedMask", "false")
    # *********************
    # * Output settings
    # *********************
    #Default pixel value for pixels that come from outside the picture:
    param_obj.SetParameter("DefaultPixelValue", "0")
    # Choose whether to generate the deformed moving image.
    # You can save some time by setting this to false, if you are
    # not interested in the final deformed moving image, but only
    # want to analyze the deformation field for example.
    param_obj.SetParameter("WriteResultImage", "true")
    # The pixel type and format of the resulting deformed moving image
    param_obj.SetParameter("ResultImagePixelType", "float")
    param_obj.SetParameter("ResultImageFormat", "mhd")
    
    return param_obj

