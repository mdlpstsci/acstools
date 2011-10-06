"""
Functions to apply pixel-based CTE correction to ACS images.

The algorithm implemented in this code was
described in detail by [Anderson]_ as available
online at:

http://adsabs.harvard.edu/abs/2010PASP..122.1035A

:Authors: Pey Lian Lim and W.J. Hack (Python), J. Anderson (Fortran), Matt Davis

:Organization: Space Telescope Science Institute

:History:
    * 2010/09/01 PLL created this module.
    * 2010/10/13 WH added/modified documentations.
    * 2010/10/15 PLL fixed PCTEFILE lookup logic.
    * 2010/10/26 WH added support for multiple file processing
    * 2010/11/09 PLL modified `YCte`, `_PixCteParams` and `_DecomposeRN` to reflect noise improvement by JA. Also updated documentations.
    * 2011/04/26 MRD Began updates for new CTE algorithm.
    * 2011/07/20 MRD Updated to handle new PCTETAB containing time dependent
      CTE characterization.

References
----------
.. [Anderson] Anderson J. & Bedin, L.R., 2010, PASP, 122, 1035

Notes
------
* This code only works for ACS/WFC but can be modified to work on other detectors.
* It was developed for use with full-frame GAIN=2 FLT images as input.
* It has not been fully tested with any other formats.
* Noise is slightly enhanced in the output (see [Anderson]_).
* This code assumes a linear time dependence for a given set of coefficients.
* This algorithm does not account for traps with very long release timescale 
  but it is not an issue for ACS/WFC.
* This code also does not account for second-exposure effect.
* Multi-threading support was not implemented in this version as it would 
  interfere with eventual pipeline operation.

"""

# External modules
import os, shutil, time, numpy, pyfits

try:
    from stsci.tools import teal
except:
    teal = None

from stsci.tools import parseinput

# Local modules
import ImageOpByAmp
import PixCte_FixY as pcfy # C extension

__taskname__ = "PixCteCorr"
__version__ = "0.5.0"
__vdate__ = "20-Jul-2011"

# constants related to the CTE algorithm in use
ACS_CTE_NAME = 'PixelCTE 2011'
ACS_CTE_VER = '2.0'

# general error for things related to his module
class PixCteError(Exception):
    pass

#--------------------------
def CteCorr(input, outFits='', noise=1, intermediateFiles=False):
    """
    Run all the CTE corrections on all the input files.
    
    This function simply calls `YCte()` on each input image
    parsed from the `input` parameter, and passes all remaining
    parameter values through unchanged.

    Parameters
    ----------
    input : str or list of str
        name of FLT image(s) to be corrected. The name(s) can be specified
        either as:
         
          * a single filename ('j1234567q_flt.fits')
          * a Python list of filenames
          * a partial filename with wildcards ('\*flt.fits')
          * filename of an ASN table ('j12345670_asn.fits')
          * an at-file ('@input')
        
    outFits : str
        *USE DEFAULT IF `input` HAS MULTIPLE FILES.*
        CTE corrected image in the same
        directory as input. If not given, will use
        ROOTNAME_cte.fits instead. Existing file will
        be overwritten.

    noise : int
        Noise mitigation algorithm. As CTE
        loss occurs before noise is added at readout,
        not removing noise prior to CTE correction
        will enhance the noise in output image.  
         
            - 0: None.
            - 1: Smoothing

    intermediateFiles : bool 
        Generate intermediate files in the same directory as input? 
        Useful for debugging. These are:
            
            1. ROOTNAME_cte_rn_tmp.fits - Noise image.
            2. ROOTNAME_cte_wo_tmp.fits - Noiseless
               image.
            3. ROOTNAME_cte_log.txt - Log file.
            
    Examples
    --------
    1.  This task can be used to correct a set of ACS images simply with:

            >>> import PixCteCorr
            >>> PixCteCorr.CteCorr('j*q_flt.fits')

        This task will generate a new CTE-corrected image for each of the FLT images.

    2.  The TEAL GUI can be used to run this task using:

            >>> epar PixCteCorr  # under PyRAF only

        or from a general Python command line:

            >>> from stsci.tools import teal
            >>> teal.teal('PixCteCorr')

    """
    # Parse input to get list of filenames to process
    infiles, output = parseinput.parseinput(input)
    
    # Process each file
    for file in infiles:
        YCte(file, outFits=outFits, noise=noise, intermediateFiles=intermediateFiles)
        
#--------------------------
def XCte():
    """
    *FUTURE WORK.*
    Not Implemented yet.
    
    Apply correction to serial CTE loss. This is to
    be done before parallel CTE loss correction.
    
    Probably easier to call as routine from `YCte()`.
    """

    raise NotImplementedError('XCte not yet implemented.')

#--------------------------
def YCte(inFits, outFits='', noise=1, intermediateFiles=False):
    """
    Apply correction for parallel CTE loss.

    Input image that is already de-striped is desired
    but not compulsory. Using image with striping
    will enhance the stripes in output. Calibrations
    that have been applied to FLT should not
    significantly affect the result.

    Notes
    -----
    * EXT 0 header will be updated. ERR arrays will be
      added in quadrature with 10% of the correction.
      DQ not changed.

    * Does not work on RAW but can be modified
      to do so.

    Parameters
    ----------
    inFits : str
        FLT image to be corrected.

    outFits : str
        CTE corrected image in the same
        directory as input. If not given, will use
        ROOTNAME_cte.fits instead. Existing file will
        be overwritten.

    noise  int
        Noise mitigation algorithm. As CTE
        loss occurs before noise is added at readout,
        not removing noise prior to CTE correction
        will enhance the noise in output image.  
         
            - 0: None.
            - 1: Smoothing

    intermediateFiles : bool 
        Generate intermediate
        files in the same directory as input? Useful
        for debugging. These are:
            
            1. ROOTNAME_cte_rn_tmp.fits - Noise image.
            2. ROOTNAME_cte_wo_tmp.fits - Noiseless
               image.
            3. ROOTNAME_cte_log.txt - Log file.
            
    Examples
    --------
    1.  This task can be used to correct a single FLT image with:

            >>> import PixCteCorr
            >>> PixCteCorr.YCte('j12345678_flt.fits')

        This task will generate a new CTE-corrected image.

    """

    # Start timer
    timeBeg = time.time()
    
    # For output files naming.
    # Store in same path as input.
    outPath = os.path.dirname( os.path.abspath(inFits) ) + os.sep
    rootname = pyfits.getval(inFits, 'ROOTNAME')
    print os.linesep, 'Performing pixel-based CTE correction on', rootname
    rootname = outPath + rootname
    
    # Construct output filename
    if not outFits: 
        outFits = rootname + '_cte.fits'

    # Copy input to output
    shutil.copyfile(inFits, outFits)

    # Open output for correction
    pf_out = pyfits.open(outFits, mode='update')

    # For detector-specific operations
    detector = pf_out['PRIMARY'].header['DETECTOR']

    # For epoch-specific operations
    expstart = pf_out['PRIMARY'].header['EXPSTART']

    # This is just for WFC for now.
    if detector != 'WFC':
        raise PixCteError('Invalid detector: PixCteCorr only supports ACS WFC.')

    # Read CTE params from file
    pctefile = pf_out['PRIMARY'].header['PCTETAB']
    cte_frac, sim_nit, shft_nit, rn_clip, q_dtde, dtde_l, psi_node, chg_leak, levels = \
      _PixCteParams(pctefile, expstart)

    # N in charge tail
    chg_leak_kt, chg_open_kt = pcfy.InterpolatePsi(chg_leak, psi_node)
    del chg_leak, psi_node

    # dtde_q: Marginal PHI at a given chg level.
    # q_pix_array: Maps Q (cumulative charge) to P (dependent var).
    # pix_q_array: Maps P to Q.
    dtde_q = pcfy.InterpolatePhi(dtde_l, q_dtde, shft_nit)
    del dtde_l, q_dtde
 
    # finish interpolation along the Q dimension and reduce arrays to contain
    # only info at the levels specified in the levels array
    chg_leak_lt, chg_open_lt, dpde_l, tail_len = \
      pcfy.FillLevelArrays(chg_leak_kt, chg_open_kt, dtde_q, levels)
    del chg_leak_kt, chg_open_kt, dtde_q
          
    # Compute open spaces. Overwrite log file.
#    chg_leak_tq, chg_open_tq = _TrackChargeTrap(pix_q_array, chg_leak_kt, 
#                                                ycte_qmax, pFile=outLog, psiNode=psi_node)
    
    # Extract data for amp quadrants.
    # For each amp, view of image is created with amp on bottom left.
    quadObj = ImageOpByAmp.ImageOpByAmp(pf_out)
    ampList = quadObj.GetAmps()
    # DQ needs to be read if new flags are to be added.
    sciQuadData = quadObj.DataByAmp()
    errQuadData = quadObj.DataByAmp(extName='ERR')

    # Intermediate files
    outLog = ''
    if intermediateFiles:
        # Images
        mosWo = quadObj.MosaicTemplate()
        mosRn = mosWo.copy()

        # Log file name
        outLog = rootname + '_cte_log.txt'
    # End if

    # Choose one amp to log detailed results
    ampPriorityOrder = ['C','D','A','B'] # Might be instrument dependent
    amp2log = ''
    for amp in ampPriorityOrder:
        if amp in ampList:
            amp2log = amp
            break
    # End for
    
    # Process each amp readout
    for amp in ampList:
        print os.linesep, 'AMP', amp
        
        # Keep a copy of original SCI for error calculations.
        # Assume unit of electrons.
        sciAmpOrig = sciQuadData[amp].copy().astype('float')
        
        # Separate noise and signal.
        # Must be in unit of electrons.
        if noise == 1:
          sciAmpSig, sciAmpNse = pcfy.DecomposeRN(sciAmpOrig, rn_clip)
        elif noise == 0:
          sciAmpSig = sciAmpOrig.copy()
          sciAmpNse = numpy.zeros(sciAmpSig.shape,dtype=sciAmpSig.dtype)
        else:
          raise PixCteError('Invalid noise model specified, must be 0 or 1.')
        
        if intermediateFiles:
            mosX1, mosX2, mosY1, mosY2, tCode = quadObj.MosaicPars(amp)
            mosWo[mosY1:mosY2,mosX1:mosX2] = quadObj.FlipAmp(sciAmpSig, tCode, trueCopy=True)
            mosRn[mosY1:mosY2,mosX1:mosX2] = quadObj.FlipAmp(sciAmpNse, tCode, trueCopy=True)
        # End if

        # Only log pre-selected amp.
        if amp == amp2log:
            outLog2 = outLog
        else:
            outLog2 = ''
        # End if
        
        # CTE correction
        sciAmpCor = pcfy.FixYCte(sciAmpSig, cte_frac, sim_nit, shft_nit,
                                  levels, dpde_l, tail_len,
                                  chg_leak_lt, chg_open_lt, amp, outLog2)
        
        # Add noise in electrons back to corrected image.
        sciAmpFin = sciAmpCor + sciAmpNse
        del sciAmpCor, sciAmpNse
        sciQuadData[amp][:,:] = sciAmpFin.astype(sciQuadData[amp].dtype)
        
        # Apply 10% correction to ERR in quadrature.
        # Assume unit of electrons.
        dcte = 0.1 * numpy.abs(sciAmpFin - sciAmpOrig)
        del sciAmpFin, sciAmpOrig
        errAmpSig = errQuadData[amp].copy().astype('float')
        errAmpFin = numpy.sqrt(errAmpSig**2 + dcte**2)
        del errAmpSig, dcte
        
        errQuadData[amp][:,:] = errAmpFin.astype(errQuadData[amp].dtype)
        del errAmpFin
        
    # End of amp loop

    # Update header
    pf_out['PRIMARY'].header.update('PCTECORR', 'COMPLETE')
    pf_out['PRIMARY'].header.update('PCTEFRAC', cte_frac)
    pf_out['PRIMARY'].header.update('PCTERNCL', rn_clip)
    pf_out['PRIMARY'].header.update('PCTESMIT', sim_nit)
    pf_out['PRIMARY'].header.update('PCTESHFT', shft_nit)
    pf_out['PRIMARY'].header.update('CTE_NAME', ACS_CTE_NAME, 'name of CTE algorithm')
    pf_out['PRIMARY'].header.update('CTE_VER', ACS_CTE_VER, 'version of CTE algorithm')
    pf_out['PRIMARY'].header.add_history('PCTE noise model is %i' % noise)
    pf_out['PRIMARY'].header.add_history('PCTECORR complete ...')

    # Close output file
    pf_out.close()
    print os.linesep, outFits, 'written'

    # Write intermediate files
    if intermediateFiles:
        outWo = rootname + '_cte_wo_tmp.fits'
        hdu = pyfits.PrimaryHDU(mosWo)
        hdu.writeto(outWo, clobber=True) # Overwrite
        
        outRn = rootname + '_cte_rn_tmp.fits'
        hdu = pyfits.PrimaryHDU(mosRn)
        hdu.writeto(outRn, clobber=True) # Overwrite

        print os.linesep, 'Intermediate files:'
        print outWo
        print outRn
        print outLog
    # End if

    # Stop timer
    timeEnd = time.time()
    print os.linesep, 'Run time:', timeEnd - timeBeg, 'secs'

#--------------------------
def _PixCteParams(fitsTable, expstart):
    """
    Read params from PCTEFILE.

    .. note: Environment variable pointing to
             reference file directory must exist.

    Parameters
    ----------
    fitsTable : str
        PCTEFILE from header.
        
    expstart : float
        MJD of exposure start time, EXPSTART in image header

    Returns
    -------
    sim_nit : int
        Number of readout simulations to do for each column of data
        
    shft_nit : int
        Number of shifts to break each readout simulation into
        
    rn_clip : float
        Maximum amplitude of read noise removed by DecomposeRN.
        
    dtde_q : ndarray
        Charge levels at which dtde_l is parameterized
    
    dtde_l : ndarray
        PHI(Q).

    psi_node : ndarray
        N values for PSI(Q,N).

    chg_leak : ndarray
        PSI(Q,N).
        
    levels : ndarray
        Charge levels at which to do CTE evaluation

    """

    # Resolve path to PCTEFILE
    refFile = _ResolveRefFile(fitsTable)
    if not os.path.isfile(refFile): 
        raise IOError, 'PCTEFILE not found: %s' % refFile

    # Open FITS table
    pf_ref = pyfits.open(refFile)

    # Read RN_CLIP value from header
    rn_clip = pf_ref['PRIMARY'].header['RN_CLIP']
    
    # read SIM_NIT value from header
    sim_nit = pf_ref['PRIMARY'].header['SIM_NIT']
    
    # read SHFT_NIT value from header
    shft_nit = pf_ref['PRIMARY'].header['SHFT_NIT']
    
    # read number of CHG_LEAK# extensions from the header
    nchg_leak = pf_ref['PRIMARY'].header['NCHGLEAK']

    # read dtde data from DTDE extension
    dtde_l = pf_ref['DTDE'].data['DTDE']
    q_dtde = pf_ref['DTDE'].data['Q']
    
    # read levels data from LEVELS extension
    levels = pf_ref['LEVELS'].data['LEVEL']
    
    # read scale data from CTE_SCALE extension
    scalemjd = pf_ref['CTE_SCALE'].data['MJD']
    scaleval = pf_ref['CTE_SCALE'].data['SCALE']
    
    cte_frac = _CalcCteFrac(expstart, scalemjd, scaleval)
    
    # there are nchg_leak CHG_LEAK# extensions. we need to find out which one
    # is the right one for our data.
    chg_leak_names = ['CHG_LEAK{}'.format(i) for i in range(1,nchg_leak+1)]
    
    for n in chg_leak_names:
        mjd1 = pf_ref[n].header['MJD1']
        mjd2 = pf_ref[n].header['MJD2']
        
        if (expstart >= mjd1) and (expstart < mjd2):
            # read chg_leak data from CHG_LEAK extension
            psi_node = pf_ref[n].data['NODE']
            chg_leak = numpy.array(pf_ref[n].data.tolist(), dtype=numpy.float32)[:,1:]
            break

    # Close FITS table
    pf_ref.close()

    return cte_frac, sim_nit, shft_nit, rn_clip, q_dtde, dtde_l, psi_node, chg_leak, levels

#--------------------------
def _ResolveRefFile(refText, sep='$'):
    """
    Resolve the full path to reference file.
    This could be replaced with existing STSDAS
    library function, if necessary.

    Assume standard syntax: dir$file.fits

    Parameters
    ----------
    refText : str
        The text to process.

    sep : char 
        Separator between directory and file name.

    Returns
    -------
    f : str
        Full path to reference file.
    """

    s = refText.split(sep)
    n = len(s)
    if n > 1:
        p = os.getenv(s[0])
        if p:
            p += os.sep
        else:
            p = ''
        # End if
        f = p + s[1]
    else:
        f = os.path.abspath(refText)
    # End if
    return f

#--------------------------
def _CalcCteFrac(expstart, scalemjd, scaleval):
    """
    Calculate CTE_FRAC used to scale CTE according to time dependence.

    Parameters
    ----------
    expstart : float
        EXPSTART from header.

    scalemjd : ndarray
        MJD points for corresponding CTE scale values in scaleval
        
    scaleval : ndarray
        CTE scale values corresponding to MJDs in scalemjd

    Returns
    -------
    cte_frac : float
        Time scaling factor.
    """
    
    # Calculate CTE_FRAC
    cte_frac = pcfy.CalcCteFrac(expstart, scalemjd, scaleval)
          
    return cte_frac
    
#--------------------------
def _InterpolatePsi(chg_leak, psi_node):
    """
    Interpolates the `PSI(Q,N)` curve at all N from
    1 to 100.

    `PSI(Q,N)` models the CTE tail profile across N
    pixels from the original pixel for a given
    charge, Q. Up to 100 pixels are tracked. For
    post-SM4 ACS/WFC, CTE loss is within 60 pixels.
    Might be worse for WFPC2 since it is older and
    has faster readout time.

    .. note: As this model is refined, future release
             might only have PSI(N) independent of Q.

    Parameters
    ----------
    chg_leak : ndarray
        PSI table data from PCTEFILE.

    psi_node : ndarray
        PSI node data from PCTEFILE.

    Returns
    -------
    chg_leak : ndarray
        Interpolated PSI.
        
    chg_open : ndarray
        Interpolated tail profile data.

    """
    
    chg_leak, chg_open = pcfy.InterpolatePsi(chg_leak, psi_node.astype(numpy.int32))
    
    return chg_leak, chg_open
    
#--------------------------
def _InterpolatePhi(dtde_l, q_dtde, shft_nit):
    """
    Interpolates the `PHI(Q)` at all Q from 1 to
    99999 (log scale).

    `PHI(Q)` models the amount of charge in CTE
    tail, i.e., probability of an electron being
    grabbed by a charge trap.
    
    Parameters
    ----------
    dtde_l : ndarray
        PHI data from PCTEFILE.

    q_dtde : ndarray
        Q levels at which dtde_l is defined, read from PCTEFILE
        
    shft_int : int
        Number of shifts performed reading out CCD

    Returns
    -------
    dtde_q : ndarray
        dtde_l interpolated at all PHI levels
    
    """
    
    dtde_q = pcfy.InterpolatePhi(dtde_l, q_dtde, shft_nit)
    
    return dtde_q
    
def _FillLevelArrays(chg_leak, chg_open, dtde_q, levels):
    """
    Interpolates CTE parameters to the charge levels specified in levels.
    
    Parameters
    ----------
    chg_leak : ndarray
        Interpolated chg_leak tail profile data returned by _InterpolatePsi.
        
    chg_open : ndarray
        Interpolated chg_open tail profile data returned by _InterpolatePsi.
        
    dtde_q : ndarray
        PHI data interpolated at all PHI levels as returned by
        _InterpolatePhi.
        
    levels : ndarray
        Charge levels at which output arrays will be interpolated.
        Read from PCTEFILE.
        
    Returns
    -------
    chg_leak_lt : ndarray
        chg_leak tail profile data interpolated at the specified charge levels.
        
    chg_open_lt : ndarray
        chg_open tail profile data interpolated at the specified charge levels.
        
    dpde_l : ndarray
        dtde_q interpolated and summed for the specified charge levels.
        
    tail_len : ndarray
        Array of maximum tail lengths for the specified charge levels.
    
    """
    
    chg_leak_lt, chg_open_lt, dpde_l, tail_len = \
      pcfy.FillLevelArrays(chg_leak, chg_open, dtde_q, levels)
      
    return chg_leak_lt, chg_open_lt, dpde_l, tail_len
    
#--------------------------
def _DecomposeRN(data_e, rn_clip=10.0):
    """
    Separate noise and signal.
    
        REAL DATA = SIGNAL + NOISE

    .. note: Assume data only has 1 amp readout with
             amp on lower left when displayed with default
             plot settings.

    Parameters
    ----------
    data_e : ndarray
        SCI data in electrons.

    rn_clip : float
        Maximum amplitude of read noise removed.
        Defaults to 10.0.

    Returns
    -------
    sigArr : ndarray
        Noiseless signal component in electrons.

    nseArr : ndarray
        Noise component in electrons.

    """
    
    sigArr, nseArr = pcfy.DecomposeRN(data_e, rn_clip)

    return sigArr, nseArr
    
def _FixYCte(detector, cte_data, cte_frac, sim_nit, shft_nit, levels, dpde_l, 
              tail_len, chg_leak_lt, chg_open_lt, amp='', outLog2=''):
    """
    Perform CTE correction on input data. It is best to perform some kind
    of readnoise smoothing on the data, otherwise the CTE algorithm will
    amplify the read noise. (In the read out process readnoise is added to
    the data after CTE blurring.)
    
    Parameters
    ----------
    detector : str
        DETECTOR from header.
        Currently only 'WFC' is supported.
        
    cte_data : ndarray
        Data in need of CTE correction. For proper results cte_data[0,x] should
        be next to the readout register and cte_data[-1,x] should be furthest.
        Data are processed a column at a time, e.g. cte_data[:,x] is corrected,
        then cte_data[:,x+1] and so on.
        
    cte_frac : float
        Time dependent CTE scaling parameter.
        
    sim_nit : int
        Number of readout simulation iterations to perform.
        
    shft_nit : int
        Number of readout shifts to do.
        
    levels : ndarray
        Levels at which CTE is evaluated as read from PCTEFILE.
        
    dpde_l : ndarray
        Parameterized amount of charge in CTE trails as a function of
        specific charge levels, as returned by _FillLevelArrays.
        
    tail_len : ndarray
        Maximum tail lengths for CTE tails at the specific charge levels
        specified by levels, as returned by _FillLevelArrays.
        
    chg_leak_lt : ndarray
        Tail profile data at charge levels specified by levels, as returned
        by _FillLevelArrays.
        
    chg_open_lt : ndarray
        Tail profile data at charge levels specified by levels, as returned
        by _FillLevelArrays.
        
    amp : char
        Amp name for this data, used in log file.
        Optional, but must be specified if outLog2 is specified.
        
    outLog2 : str
        Name of optional log file.
        
    Returns
    -------
    corrected : ndarray
        Data CTE correction algorithm applied. Same size and shape as input
        cte_data.
    
    """
    
    if outLog2 != '' and amp == '':
        raise PixCteError('amp argument must be specified if log file is specified.')

    if detector == 'WFC':
        corrected = pcfy.FixYCte(cte_data, cte_frac, sim_nit, shft_nit,
                                levels, dpde_l, tail_len,
                                chg_leak_lt, chg_open_lt, amp, outLog2)
    else:
        raise PixCteError('Invalid detector: PixCteCorr only supports ACS WFC.')
                              
    return corrected


def AddYCte(infile, outfile, units=None):
    """
    Add CTE blurring to input image using an inversion of the CTE correction
    code.
    
    .. note: No changes are made to the error or data quality arrays. 
    
             Data should not have bias or prescan regions.
             
             Image must have PCTETAB, DETECTOR, and EXPSTART
             header keywords, as well as gain information if the image
             is in counts.
    
    Paramters
    ---------
    infile : str
        Filename of image to be blurred. Should have the PCTETAB header
        keyword pointing to the PCTETAB reference file.
        
    outfile : str
        Filename of blurred output image.
        
    units : {None,'electrons','counts'}, optional
        If 'electrons', the input image is assumed to have units of electrons
        and no gain operations are performed.
        If 'counts', the data are assumed to be in DN and they are converted
        to electrons before CTE blurring is performed. The ATODGN* keywords
        from the primary header are used for the conversions.
        If None, the BUNIT keyword from the science extension headers is used
        to set the unit behavior.
        Defaults to None.
        
    Raises
    ------
    ValueError
        If the units keyword is not a valid value.
    
    acstools.PixCteCorr.PixCteError
        If the input image comes from an imcompatible detector.
    
    """
    # check the units keyword
    if units not in (None,'electrons','counts'):
        raise ValueError("units keyword must be one of (None,'electrons','counts')")
    
    # copy infile to outfile
    shutil.copyfile(infile, outfile)
    
    # open file for blurring
    fits = pyfits.open(outfile, mode='update')
    
    # For detector-specific operations
    detector = fits['PRIMARY'].header['DETECTOR']

    # For epoch-specific operations
    expstart = fits['PRIMARY'].header['EXPSTART']

    # This is just for WFC for now.
    if detector != 'WFC':
        os.remove(outfile)
        raise PixCteError('Invalid detector: PixCteCorr only supports ACS WFC.')
        
    # get units, if necessary
    if units is None:
        units = fits[1].header['BUNIT'].strip().lower()
        
    # Read CTE params from file
    pctefile = fits['PRIMARY'].header['PCTETAB']
    cte_frac, sim_nit, shft_nit, rn_clip, q_dtde, dtde_l, psi_node, chg_leak, levels = \
      _PixCteParams(pctefile, expstart)

    # N in charge tail
    chg_leak_kt, chg_open_kt = pcfy.InterpolatePsi(chg_leak, psi_node)
    del chg_leak, psi_node

    # dtde_q: Marginal PHI at a given chg level.
    # q_pix_array: Maps Q (cumulative charge) to P (dependent var).
    # pix_q_array: Maps P to Q.
    dtde_q = pcfy.InterpolatePhi(dtde_l, q_dtde, shft_nit)
    del dtde_l, q_dtde
 
    # finish interpolation along the Q dimension and reduce arrays to contain
    # only info at the levels specified in the levels array
    chg_leak_lt, chg_open_lt, dpde_l, tail_len = \
      pcfy.FillLevelArrays(chg_leak_kt, chg_open_kt, dtde_q, levels)
    del chg_leak_kt, chg_open_kt, dtde_q
    
    ########################################
    # perform correction for chip 2 (ext. 1)
    ########################################

    # get data for chip 2 (ext. 1)
    scidata = fits[1].data.copy().astype(numpy.float)
    
    # convert to electrons if needed
    if units == 'counts':
        gainc = fits[0].header['atodgnc']
        gaind = fits[0].header['atodgnd']
        
        scidata[:,:2048] *= gainc
        scidata[:,2048:] *= gaind
        
    # call CTE blurring routine. data must be in units of electrons.
    print 'Performing CTE blurring for science extension 1.'
    
    t1 = time.time()
    cordata = _AddYCte(detector, scidata, cte_frac, shft_nit,
                        levels, dpde_l, tail_len, chg_leak_lt, chg_open_lt)
    t2 = time.time()

    print 'AddYCte took {} seconds for science extension 1.'.format(t2-t1)
    
    # convert blurred data back to DN
    if units == 'counts':
        cordata[:,:2048] /= gainc
        cordata[:,2048:] /= gaind

    # copy blurred data back to image.
    fits[1].data[:,:] = cordata.astype(numpy.float32)[:,:]
    
    ########################################
    # perform correction for chip 1 (ext. 4)
    ########################################

    # get data for chip 1 (ext. 4)
    scidata = fits[4].data.copy().astype(numpy.float)
    
    # convert to electrons
    if units == 'counts':
        gaina = fits[0].header['atodgna']
        gainb = fits[0].header['atodgnb']
        
        scidata[:,:2048] *= gaina
        scidata[:,2048:] *= gainb
    
    # data needs to be flipped so that row 0 is closest to the readout, since
    # that's what the algorithm expects
    scidata = scidata[::-1,:]

    # call CTE blurring routine. data must be in units of electrons.
    print 'Performing CTE blurring for science extension 2.'
    
    t1 = time.time()
    cordata = _AddYCte(detector, scidata, cte_frac, shft_nit,
                        levels, dpde_l, tail_len, chg_leak_lt, chg_open_lt)
    t2 = time.time()

    print 'AddYCte took {} seconds for science extension 2.'.format(t2-t1)
    
    # convert blurred data back to DN
    if units == 'counts':
        cordata[:,:2048] /= gaina
        cordata[:,2048:] /= gainb
    
    # flip data back arround to its original orientation
    cordata = cordata[::-1,:]

    # copy blurred data back to image.
    fits[4].data[:,:] = cordata.astype(numpy.float32)[:,:]
    
    # Update header
    fits['PRIMARY'].header.update('PCTEFRAC', cte_frac)
    fits['PRIMARY'].header.update('PCTERNCL', rn_clip)
    fits['PRIMARY'].header.update('PCTESMIT', sim_nit)
    fits['PRIMARY'].header.update('PCTESHFT', shft_nit)
    fits['PRIMARY'].header.add_history('CTE blurring performed by PixCteCorr.AddYCte')
    
    # close image
    fits.close()

    
def _AddYCte(detector, input_data, cte_frac, shft_nit, levels, dpde_l, 
              tail_len, chg_leak_lt, chg_open_lt):
    """
    Apply ACS CTE blurring to input data.
    
    Parameters
    ----------
    detector : str
        DETECTOR from header.
        Currently only 'WFC' is supported.
        
    input_data : ndarray
        Data in need of CTE correction. For proper results cte_data[0,x] should
        be next to the readout register and cte_data[-1,x] should be furthest.
        Data are processed a column at a time, e.g. cte_data[:,x] is corrected,
        then cte_data[:,x+1] and so on.
        
    cte_frac : float
        Time dependent CTE scaling parameter.
        
    shft_nit : int
        Number of readout shifts to do.
        
    levels : ndarray
        Levels at which CTE is evaluated as read from PCTEFILE.
        
    dpde_l : ndarray
        Parameterized amount of charge in CTE trails as a function of
        specific charge levels, as returned by _FillLevelArrays.
        
    tail_len : ndarray
        Maximum tail lengths for CTE tails at the specific charge levels
        specified by levels, as returned by _FillLevelArrays.
        
    chg_leak_lt : ndarray
        Tail profile data at charge levels specified by levels, as returned
        by _FillLevelArrays.
        
    chg_open_lt : ndarray
        Tail profile data at charge levels specified by levels, as returned
        by _FillLevelArrays.
        
    Returns
    -------
    blurred : ndarray
        Data CTE correction algorithm applied. 
        Same size and shape as input_data.
    
    """
    
    if detector == 'WFC':
        blurred = pcfy.AddYCte(input_data, cte_frac,shft_nit,
                                levels, dpde_l, tail_len,
                                chg_leak_lt, chg_open_lt)
    else:
        raise PixCteError('Invalid detector: PixCteCorr only supports ACS WFC.')
                              
    return blurred

#--------------------------
# TEAL Interface functions
#--------------------------
def run(configObj):
    
    CteCorr(configObj['inFits'],outFits=configObj['outFits'],noise=configObj['noise'],
        intermediateFiles=configObj['debug'])
    
def getHelpAsString():
    helpString = ''
    if teal:
        helpString += teal.getHelpFileAsString(__taskname__,__file__)

    if helpString.strip() == '':
        helpString += __doc__+'\n'+YCte.__doc__

    return helpString