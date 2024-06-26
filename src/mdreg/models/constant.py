
import numpy as np

def pars():
    return ['const']

def main(images):
    """ main function that performs the T2*-map signal model-fit for input 2D image at multiple time-points (TEs).

    Args
    ----
    images (numpy.ndarray): input image at all time-series (i.e. at each TE time) with shape [x-dim*y-dim, total time-series].    

    Returns
    -------
    fit (numpy.ndarray): signal model fit per pixel for whole image with shape [x-dim*y-dim, total time-series].  
    par (numpy.ndarray): output signal model fit parameters 'S' and 'R' stored in a single nd-array with shape [2, x-dim*y-dim].      
    """

    shape = np.shape(images)
    avr = np.mean(images, axis=1) # fitting a constant model
    par = np.empty((shape[0], 1)) 
    par[:,0] = avr
    fit = np.repeat(avr[:,np.newaxis], shape[1], axis=1)
    
    return fit, par




