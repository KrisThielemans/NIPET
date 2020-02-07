"""Voxel-driven scatter modelling for PET data"""
from concurrent.futures import ThreadPoolExecutor
import logging
from math import pi
import os
import random
import re
import scipy.ndimage as ndi
import sys

import nibabel as nib
import numpy as np
from scipy.interpolate import CloughTocher2DInterpolator
import scipy.spatial.qhull as qhull

from ..img import mmrimg
from .. import mmraux
from .. import mmr_auxe
from .. import mmrnorm
from . import nifty_scatter
from ..prj import mmrprj, petprj, mmrrec
__author__      = ("Pawel J. Markiewicz", "Casper O. da Costa-Luis")
__copyright__   = "Copyright 2020"
log = logging.getLogger(__name__)


def fwhm2sig (fwhm, Cnt):
    return (fwhm/Cnt['SO_VXY']) / (2*(2*np.log(2))**.5)


#=================================================================================================
# S C A T T E R
#-------------------------------------------------------------------------------------------------


def get_scrystals(scanner_params):
    """
    get table of selected transaxial and axial (ring) crystals
    for scatter modelling
    """
    #> decompose constants, transaxial and axial LUTs are extracted
    Cnt   = scanner_params['Cnt']
    txLUT = scanner_params['txLUT']
    axLUT = scanner_params['axLUT']

    #------------------------------------------------------
    #> transaxial crystals definitions
    crs = txLUT['crs']

    #> period of scatter crystals (needed for definition)
    SCRS_T = 7

    #> counter for crystal period, SCRS_T
    cntr = 0

    #> scatter crystal index
    iscrs = 0

    #> initialise list of transaxial scatter crystal table
    scrs = []

    logtxt = ''

    #> transaxial scatter crystal selection for modelling
    for c in range(Cnt['NCRS']):
        if (((c + 1) % 9) == 0): continue
        cntr += 1
        if (cntr == SCRS_T):
            cntr = 0
            scrs.append([
                c,
                0.5*(crs[0, c] + crs[2, c]),
                0.5*(crs[1, c] + crs[3, c])
                ])

            logtxt += '''\
                \r{}-th scatter crystal is #{}: (x,y) = {}, {}
                '''.format(iscrs, c, scrs[-1][1], scrs[-1][2])
            iscrs += 1

    log.info('transaxial scatter crystal definitions:\n\n'+logtxt)

    #> convert the scatter crystal table to Numpy array
    scrs = np.array(scrs, dtype=np.float32)
    #------------------------------------------------------


    #------------------------------------------------------
    #> scatter ring definition (axially)
    sct_irng = np.int16([0, 10, 19, 28, 35, 44, 53, 63])
    # number of scatter rings (used for scatter estimation)
    NSRNG = len(sct_irng)
    #------------------------------------------------------

    return dict(SCTRNG=sct_irng, NSRNG=NSRNG, SCTCRS=scrs)


def get_knlut(Cnt):
    """get Klein-Nishina LUTs"""
    from scipy.special import erfc
    SIG511 = Cnt['ER']*Cnt['E511']/2.35482

    CRSSavg = (2*(4/3.0-np.log(3)) + .5*np.log(3)-4/9.0)

    knlut = np.zeros((Cnt['NCOS'],2), dtype = np.float32)

    for i in range(Cnt['NCOS']):
        cosups = (Cnt['COSUPSMX']+i*Cnt['COSSTP'])
        alpha = 1/(2 - cosups)
        KNtmp = ( (0.5*Cnt['R02']) * alpha*alpha * ( alpha + 1/alpha - (1-cosups*cosups) ) )
        knlut[i,0] = KNtmp / ( 2*pi*Cnt['R02'] * CRSSavg);
        knlut[i,1] = ( (1+alpha)/(alpha*alpha)*(2*(1+alpha)/(1+2*alpha)-1/alpha*np.log(1+2*alpha)) + \
                        np.log(1+2*alpha)/(2*alpha)-(1+3*alpha)/((1+2*alpha)*(1+2*alpha)) ) / CRSSavg

        # Add energy resolution:
        if Cnt['ER']>0:
            log.info('using energy resolution for scatter simulation, ER = {}'.format(Cnt['ER']))
            knlut[i,0] *= .5*erfc( (Cnt['LLD']-alpha*Cnt['E511'])/(SIG511*np.sqrt(2*alpha)) );
            #knlut[i,0] *= .5*erfc( (Cnt['LLD']-alpha*Cnt['E511'])/(SIG511) );

        # for large angles (small cosups) when the angle in GPU calculations is greater than COSUPSMX
        if (i==0):
            knlut[0,0] = 0;

    return knlut


#==================================================================================================
# GET SCATTER LUTs
#--------------------------------------------------------------------------------------------------


def rd2sni(offseg, r1, r0):
    rd = np.abs(r1-r0)
    rdi = (2*rd - 1*(r1>r0))
    sni = offseg[rdi] + np.minimum(r0,r1)
    return sni


def get_sctLUT(scanner_params):

    #> decompose constants, transaxial and axial LUTs are extracted
    Cnt   = scanner_params['Cnt']
    txLUT = scanner_params['txLUT']
    axLUT = scanner_params['axLUT']


    #> get the Klein-Nishina LUT:
    KN = get_knlut(Cnt)

    #> get scatter crystal tables:
    scrs_def = get_scrystals(scanner_params)

    # get the indexes of rings used for scatter estimation
    irng = scrs_def['SCTRNG']

    # get number of ring accounting for the possible ring reduction (to save computation time)
    # NRNG = Cnt['RNG_END']-Cnt['RNG_STRT']

    #-span-1 LUT (rings to sino index)
    seg = np.append( [Cnt['NRNG']], np.ceil( np.arange(Cnt['NRNG']-1,0,-.5) ).astype(np.int16) )
    offseg = np.int16( np.append( [0], np.cumsum(seg)) )

    #-3D scatter sino LUT. axial component based on michelogram.
    sctaxR = np.zeros((Cnt['NRNG']**2, 4), dtype=np.int32)
    sctaxW = np.zeros((Cnt['NRNG']**2, 4), dtype=np.float32)

    #-just for local check and display of the interpolation at work
    mich  = np.zeros((Cnt['NRNG'], Cnt['NRNG']), dtype=np.float32)
    mich2 = np.zeros((Cnt['NRNG'], Cnt['NRNG']), dtype=np.float32)


    J, I =  np.meshgrid(irng, irng)
    mich[J,I] = np.reshape(np.arange(len(scrs_def['SCTRNG'])**2), (len(scrs_def['SCTRNG']), len(scrs_def['SCTRNG'])))
    # plt.figure(64), plt.imshow(mich, interpolation='none')

    for r1 in range(Cnt['RNG_STRT'], Cnt['RNG_END']):
        #border up and down
        bd = next(idx for idx in irng        if idx>=r1)
        bu = next(idx for idx in irng[::-1]  if idx<=r1)
        for r0 in range(Cnt['RNG_STRT'], Cnt['RNG_END']):

            # if (np.abs(r1-r0)>MRD):
            #     continue
            #border left and right
            br = next(idx for idx in irng        if idx>=r0)
            bl = next(idx for idx in irng[::-1]  if idx<=r0)
            #print '(r0,r1)=', r0,r1, '(bl,br,bu,bd)', bl,br,bu,bd

            #span-1 sino index (sni) creation:
            sni = rd2sni(offseg, r1, r0)

            #see: https://en.wikipedia.org/wiki/Bilinear_interpolation
            if (br==bl)and(bu!=bd):

                sctaxR[sni,0] = rd2sni(offseg, bd, r0)
                sctaxW[sni,0] = (r1-bu)/float(bd-bu)
                sctaxR[sni,1] = rd2sni(offseg, bu, r0)
                sctaxW[sni,1] = (bd-r1)/float(bd-bu)

                mich2[r1,r0] = mich[bd,r0]*sctaxW[sni,0]  +  mich[bu,r0]*sctaxW[sni,1]

            elif (bu==bd)and(br!=bl):

                sctaxR[sni,0] = rd2sni(offseg, r1, bl)
                sctaxW[sni,0] = (br-r0)/float(br-bl)
                sctaxR[sni,1] = rd2sni(offseg, r1, br)
                sctaxW[sni,1] = (r0-bl)/float(br-bl)

                mich2[r1,r0] =  mich[r1,bl]*sctaxW[sni,0] + mich[r1,br]*sctaxW[sni,1]

            elif (bu==bd)and(br==bl):

                mich2[r1,r0] = mich[r1,r0]
                sctaxR[sni,0] = rd2sni(offseg, r1, r0)
                sctaxW[sni,0] = 1
                continue

            else:

                cf = float(((br-bl)*(bd-bu)))

                sctaxR[sni,0] = rd2sni(offseg, bd, bl)
                sctaxW[sni,0] = (br-r0)*(r1-bu)/cf
                sctaxR[sni,1] = rd2sni(offseg, bd, br)
                sctaxW[sni,1] = (r0-bl)*(r1-bu)/cf

                sctaxR[sni,2] = rd2sni(offseg, bu, bl)
                sctaxW[sni,2] = (br-r0)*(bd-r1)/cf
                sctaxR[sni,3] = rd2sni(offseg, bu, br)
                sctaxW[sni,3] = (r0-bl)*(bd-r1)/cf

                mich2[r1,r0] =  mich[bd,bl]*sctaxW[sni,0]+ mich[bd,br]*sctaxW[sni,1] + mich[bu,bl]*sctaxW[sni,2] + mich[bu,br]*sctaxW[sni,3]

    # plt.figure(65), plt.imshow(mich2, interpolation='none')

    sctLUT = {
        'sctaxR':sctaxR,
        'sctaxW':sctaxW,
        'isrng':irng,
        'offseg':offseg,
        'KN':KN,
        'mich_chck':[mich, mich2],
        **scrs_def,
        'NSCRS':scrs_def['SCTCRS'].shape[0]}

    return sctLUT


#-------------------------------------------------------------------------------------------------
# S C A T T E R    I N T E R P O L A T I O N
#-------------------------------------------------------------------------------------------------


def intrp_sct(sctind, sct3d, Cnt, snno, ssrlut, dtype=np.float32):
    '''
    perform 2D interpolation for all scatter sinograms
    to form a 3D scatter sionogram.
    '''
    #> indecies
    iAW = np.unique(sctind)
    nsbins = len(iAW)

    ai = iAW // Cnt['NSBINS']
    wi = iAW - ai*Cnt['NSBINS']

    #---add extra points (from the top) at the bottom of sino
    #find the indexes where angle indx is 0
    ai0 = np.where(ai==0)
    wie = abs((Cnt['NSBINS']-1)-wi[ai0])
    wien = len(wie)
    aie = Cnt['NSANGLES']*np.ones(wien, dtype=np.int32)

    #---prepare the array for qhull.Delaunay input
    sind = np.zeros( (wien + nsbins,2), dtype=np.int32)
    # append to the sino bottom in order to get a properly interpolated sino
    sind[:,1] = np.append(wi,wie)
    sind[:,0] = np.append(ai,aie)

    #> get the traingulations based on data points (sctind)
    tri = qhull.Delaunay(sind)

    grid_x, grid_y = np.mgrid[0:Cnt['NSANGLES'], 0:Cnt['NSBINS']]

    #-----------------------------------------------------
    log.info('scatter sinogram interpolation...')

    def func(i):
        #> temporary 2D scatter sinogram, initially as 1D vector
        tmp2d = np.zeros((Cnt['NSANGLES']*Cnt['NSBINS']), dtype=dtype)
        tmp2d[:] = 0
        for ti in range(len(sctind)):
            tmp2d[ sctind[ti] ] += sct3d[0,i,ti]

        ssn2d = np.reshape(tmp2d, (Cnt['NSANGLES'], Cnt['NSBINS']))

        #> prepare the array for interpolation input
        sval = np.zeros( (wien + nsbins,1), dtype=dtype)

        #> append to the sino bottom in order to get a properly interpolated sino
        sval = ssn2d[
            np.append( ai, np.zeros(len(ai0[0]), dtype=np.int32) ),
            np.append(wi, wi[ai0]) ]

        intrp = CloughTocher2DInterpolator(tri, sval, fill_value=0.0)
        issn = intrp((grid_x, grid_y))
        issn = np.nan_to_num(issn)
        issn[issn<0] = 0.
        return issn
    #-----------------------------------------------------

    ssn = np.zeros((snno, Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.float32)
    sssr = np.zeros((Cnt['NSEG0'], Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.float32)

    #> begin Casper's threading magic:
    with ThreadPoolExecutor() as exec:
        for i, issn in enumerate(exec.map(func, range(snno))):
            ssn[i,...] = issn
            sssr[ssrlut[i],:,:] += ssn[i,:,:]
    #> end of Casper's magic

    return ssn, sssr


# def get_sctinterp(ssn2d, sct2AW, Cnt):

#     from scipy.interpolate import griddata #used for scatter interpolation

#     iAW = np.unique(sct2AW)
#     nsbins = len(iAW)

#     ai = iAW // Cnt['NSBINS']
#     wi = iAW - ai*Cnt['NSBINS']

#     #---add extra points (from the top) at the bottom of sino
#     #find the indexes where angle indx is 0
#     ai0 = np.where(ai==0)
#     wie = abs((Cnt['NSBINS']-1)-wi[ai0])
#     wien = len(wie)
#     aie = Cnt['NSANGLES']*np.ones(wien, dtype=np.int32)

#     #---prepare the arrays for interpolation input
#     # indexes
#     sind = np.zeros( (wien + nsbins,2), dtype=np.int32)
#     # scatter values for the above indecies
#     sval = np.zeros( (wien + nsbins,1), dtype=np.float32)
#     # append to the sino bottom in order to get a properly interpolated sino
#     sind[:,1] = np.append(wi,wie)
#     sind[:,0] = np.append(ai,aie)
#     sval = ssn2d[ np.append( ai, np.zeros(len(ai0[0]),dtype=np.int32) ), np.append(wi, wi[ai0]) ]

#     # do the interpolation after creating the grid
#     grid_x, grid_y = np.mgrid[0:Cnt['NSANGLES'], 0:Cnt['NSBINS']]
#     issino = griddata(sind, sval, (grid_x, grid_y), method='cubic')
#     issino = np.nan_to_num(issino)
#     issino[issino<0] = 0

#     return issino


def vsm(
        datain,
        mumaps,
        em,
        hst,
        rsinos,
        scanner_params,
        prcnt_scl = 0.1,
        emmsk=False,
        return_uninterp=False,
        return_ssrb=False,
        return_mask=False,
    ):
    '''
    Voxel-driven scatter modelling (VSM).
    Obtain a scatter sinogram using the mu-maps (hardware and object mu-maps)
    an estimate of emission image, the prompt measured sinogram, an
    estimate of the randoms sinogram and a normalisation sinogram.
    Input:
        - datain:       Contains the data used for scatter-specific detector
                        normalisation.  May also include the non-corrected
                        emission image used for masking, when requested.
        - mumaps:       A tuple of hardware and object mu-maps (in this order).
        - em:           An estimate of the emission image.
        - hst:          Dictionary containing the histogrammed measured data into
                        sinograms.
        - rsinos:       Randoms sinogram (3D).  Needed for proper scaling of
                        scatter to the prompt data.
        - scanner_params: Scanner specific parameters.
        - prcnt_scl:    Ratio of the maximum scatter intensities below which the
                        scatter is not used for fitting it to the tails of prompt
                        data.  Default is 10%.
        - emmsk:        When 'True' it will use uncorrected emission image for
                        masking the sources (voxels) of photons to be used in the
                        scatter modelling.
    '''
    #> decompose constants, transaxial and axial LUTs are extracted
    Cnt   = scanner_params['Cnt']
    txLUT = scanner_params['txLUT']
    axLUT = scanner_params['axLUT']

    #> decompose mu-maps
    muh, muo = mumaps

    if emmsk and not os.path.isfile(datain['em_nocrr']):
        log.info('reconstructing emission data without scatter and attenuation corrections for mask generation...')
        recnac = mmrrec.osemone(datain, mumaps, hst, scanner_params, recmod=0, itr=3, fwhm=2.0, store_img=True)
        datain['em_nocrr'] = recnac.fpet


    #-get the normalisation components
    nrmcmp, nhdr = mmrnorm.get_components(datain, Cnt)

    #-smooth for defining the sino scatter only regions
    mu_sctonly =  ndi.filters.gaussian_filter(
        mmrimg.convert2dev(muo, Cnt),
        fwhm2sig(0.42, Cnt),
        mode='mirror'
    )

    if Cnt['SPN']==1:
        snno = Cnt['NSN1']
        snno_= Cnt['NSN64']
        ssrlut = axLUT['sn1_ssrb']
        saxnrm = nrmcmp['sax_f1']
    elif Cnt['SPN']==11:
        snno = Cnt['NSN11']
        snno_= snno
        ssrlut = axLUT['sn11_ssrb']
        saxnrm = nrmcmp['sax_f11']

    #LUTs for scatter
    sctLUT = get_sctLUT(scanner_params)


    #-smooth before down-sampling mu-map and emission image
    muim = ndi.filters.gaussian_filter(muo+muh, fwhm2sig(0.42, Cnt), mode='mirror')
    muim = ndi.interpolation.zoom( muim, Cnt['SCTSCLMU'], order=3 ) #(0.499, 0.5, 0.5)

    emim = ndi.filters.gaussian_filter(em, fwhm2sig(0.42, Cnt), mode='mirror')
    emim = ndi.interpolation.zoom( emim, Cnt['SCTSCLEM'], order=3 ) #(0.34, 0.33, 0.33)
    #emim = ndi.interpolation.zoom( emim, (0.499, 0.5, 0.5), order=3 )


    #-smooth the mu-map for mask creation.  the mask contains voxels for which attenuation ray LUT is found.
    smomu = ndi.filters.gaussian_filter(muim, fwhm2sig(0.84, Cnt), mode='mirror')
    mumsk = np.int8(smomu>0.003)

    #CORE SCATTER ESTIMATION
    NSCRS, NSRNG = 64, 8
    sctout ={
        'bin_indx':np.zeros((NSCRS, NSCRS//2), dtype=np.int32),
        'sct_3d'  :np.zeros((Cnt['TOFBINN'], snno_, NSCRS, NSCRS//2), dtype=np.float32),
        'sct_val' :np.zeros((Cnt['TOFBINN'], NSRNG, NSCRS, NSRNG, NSCRS//2), dtype=np.float32),
        'xsxu'    :np.zeros((NSCRS, NSCRS//2), dtype=np.int8), #one when xs>xu, otherwise zero
    }

    #<<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>>
    nifty_scatter.vsm(sctout, muim, mumsk, emim, sctLUT, txLUT, axLUT, Cnt)
    #<<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>>

    sct3d  = sctout['sct_3d']
    sctind = sctout['bin_indx']

    # import pdb; pdb.set_trace()

    log.debug('total scatter sum: {}'.format(np.sum(sct3d)))
    if np.sum(sct3d)<1e-04:
        sss    = np.zeros((snno, Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.float32);
        amsksn = np.zeros((snno, Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.float32);
        sssr   = np.zeros((Cnt['NSEG0'], Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.float32);
        return sss, sssr, amsksn

    #> get SSR for randoms from span-1 or span-11
    rssr = np.zeros((Cnt['NSEG0'], Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.float32);
    for i in range(snno):
        rssr[ssrlut[i],:,:] += rsinos[i,:,:]

    #ATTENUATION FRACTIONS for scatter only regions, and NORMALISATION for all SCATTER
    #<<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>>
    currentspan = Cnt['SPN']
    Cnt['SPN'] = 1
    atto = np.zeros((txLUT['Naw'], Cnt['NSN1']), dtype=np.float32)
    petprj.fprj(atto, mu_sctonly, txLUT, axLUT, np.array([-1], dtype=np.int32), Cnt, 1)
    atto = mmraux.putgaps(atto, txLUT, Cnt)
    #--------------------------------------------------------------
    #get norm components setting the geometry and axial to ones as they are accounted for differently
    nrmcmp['geo'][:] = 1
    nrmcmp['axe1'][:] = 1
    #get sino with no gaps
    nrmg = np.zeros((txLUT['Naw'], Cnt['NSN1']), dtype=np.float32)
    mmr_auxe.norm(nrmg, nrmcmp, hst['buckets'], axLUT, txLUT['aw2ali'], Cnt)
    nrm = mmraux.putgaps(nrmg, txLUT, Cnt)
    #--------------------------------------------------------------


    #get attenuation + norm in (span-11) and SSR
    attossr = np.zeros((Cnt['NSEG0'], Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.float32);
    nrmsssr = np.zeros((Cnt['NSEG0'], Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.float32);
    for i in range(Cnt['NSN1']):
        si = axLUT['sn1_ssrb'][i]
        attossr[si,:,:] += atto[i,:,:] / float(axLUT['sn1_ssrno'][si])
        nrmsssr[si,:,:] += nrm[i,:,:] / float(axLUT['sn1_ssrno'][si])
    if currentspan==11:
        Cnt['SPN']=11
        nrmg = np.zeros((txLUT['Naw'], snno), dtype=np.float32)
        mmr_auxe.norm(nrmg, nrmcmp, hst['buckets'], axLUT, txLUT['aw2ali'], Cnt)
        nrm = mmraux.putgaps(nrmg, txLUT, Cnt)
    #--------------------------------------------------------------

    #get the mask for the object from uncorrected emission image
    if emmsk and os.path.isfile(datain['em_nocrr']):
        nim = nib.load(datain['em_nocrr'])
        A   = nim.get_sform()
        eim = np.float32( nim.get_data() )
        eim = eim[:,::-1,::-1]
        eim = np.transpose(eim, (2, 1, 0))

        em_sctonly = ndi.filters.gaussian_filter(eim, fwhm2sig(.6, Cnt), mode='mirror')
        msk = np.float32(em_sctonly>0.07*np.max(em_sctonly))
        msk = ndi.filters.gaussian_filter(msk, fwhm2sig(.6, Cnt), mode='mirror')
        msk = np.float32(msk>0.01)
        msksn = mmrprj.frwd_prj(msk, txLUT, axLUT, Cnt)

        mssr = mmraux.sino2ssr(msksn, axLUT, Cnt)
        mssr = mssr>0
    else:
        mssr = np.zeros((Cnt['NSEG0'], Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.bool);

    #<<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>><<+>>

    #--------------------------------------------------------------------------------------------
    #> get scatter sinos for TOF or non-TOF
    if Cnt['TOFBINN']>1:
        ssn = np.zeros((Cnt['TOFBINN'], snno, Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.float64);
        sssr = np.zeros((Cnt['TOFBINN'], Cnt['NSEG0'], Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.float32);
        tmp2d = np.zeros((Cnt['NSANGLES']*Cnt['NSBINS']), dtype=np.float64)
        log.info('interpolate each scatter sino...')
        for k in range(Cnt['TOFBINN']):
            log.info('interpolating TOF bin k = {}'.format(k))
            for i in range(snno):
                tmp2d[:] = 0
                for ti in range(len(sctind)):
                    tmp2d[ sctind[ti] ] += sct3d[k,i,ti]
                #interpolate estimated scatter
                ssn[k,i,:,:] = get_sctinterp( np.reshape(tmp2d, (Cnt['NSANGLES'], Cnt['NSBINS'])), sctind, Cnt )
                sssr[k, ssrlut[i], :, :] += ssn[k,i,:,:]

    elif Cnt['TOFBINN']==1:

        ssn, sssr = intrp_sct(sctind, sct3d, Cnt, snno, ssrlut)

        # tmp2d = np.zeros((Cnt['NSANGLES']*Cnt['NSBINS']), dtype=np.float32)

        # log.info('scatter sinogram interpolation...')
        # for i in range(snno):
        #     tmp2d[:] = 0
        #     for ti in range(len(sctind)):
        #         tmp2d[ sctind[ti] ] += sct3d[0,i,ti]
        #     #interpolate estimated scatter
        #     ssn[i,:,:] = get_sctinterp( np.reshape(tmp2d, (Cnt['NSANGLES'], Cnt['NSBINS'])), sctind, Cnt )
        #     sssr[ssrlut[i],:,:] += ssn[i,:,:]
        #     if (i%100)==0:
        #         log.info('sinograms interpolated: {}'.format(i))


    #--------------------------------------------------------------------------------------------

    #=== scale scatter for ssr and non-TOF===
    #mask
    rmsk = (txLUT['msino']>0).T
    rmsk.shape = (1,Cnt['NSANGLES'],Cnt['NSBINS'])
    rmsk = np.repeat(rmsk, Cnt['NSEG0'], axis=0)
    amsksn = np.logical_and( attossr>=0.999, rmsk) * ~mssr
    #scaling factors for ssr
    scl_ssr = np.zeros( (Cnt['NSEG0']), dtype=np.float32)
    for sni in range(Cnt['NSEG0']):
        # region of choice for scaling
        thrshld = prcnt_scl * np.max(sssr[sni,:,:])
        amsksn[sni,:,:] *= (sssr[sni,:,:]>thrshld)
        amsk = amsksn[sni,:,:]
        #normalised estimated scatter
        mssn = sssr[sni,:,:] * nrmsssr[sni,:,:]
        mssn[np.invert(amsk)] = 0
        #vectorised masked sino
        vssn = mssn[amsk]
        vpsn = hst['pssr'][sni, amsk] - rssr[sni, amsk]
        scl_ssr[sni] = np.sum(vpsn) / np.sum(mssn)
        #ssr output
        sssr[sni,:,:] *= nrmsssr[sni,:,:]*scl_ssr[sni]


    #=== scale scatter for the proper sino ===
    sss = np.zeros((snno, Cnt['NSANGLES'], Cnt['NSBINS']), dtype=np.float32);
    for i in range(snno):
        sss[i,:,:] = ssn[i,:,:]*scl_ssr[ssrlut[i]]*saxnrm[i] * nrm[i,:,:]

    out = {}

    if return_uninterp:
        out['uninterp'] = sct3d
        out['indexes'] = sctind

    if return_ssrb:
        out['ssrb'] = sssr

    if return_mask:
        out['mask'] = amsk


    if not out:
        return sss
    else:
        out['sino'] = sss
        return out
