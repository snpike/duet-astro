import os
import copy
import pickle

import numpy as np

from scipy.interpolate import interp1d

from astropy.table import Table, QTable
import astropy.constants as c
import astropy.units as u
from .duet_sensitivity import calc_snr
from .utils import get_neff, suppress_stdout, tqdm, mkdir_p
from .bbmag import sigerr
from .config import Telescope
from .background import background_pixel_rate
from .image_utils import construct_image, run_daophot, find


def join_equal_gti_boundaries(gti):
    """If the start of a GTI is right at the end of another, join them.

    """
    new_gtis = gti
    touching = gti[:-1, 1] == gti[1:, 0]
    if np.any(touching):
        ng = []
        count = 0
        while count < len(gti) - 1:
            if new_gtis[count, 1] == gti[count + 1, 0]:
                ng.append([gti[count, 0], gti[count + 1, 1]])
            else:
                ng.append(gti[count])
            count += 1
        new_gtis = np.asarray(ng)
    return new_gtis


def cross_two_gtis(gti0, gti1):
    """Extract the common intervals from two GTI lists *EXACTLY*.

    From Stingray

    Parameters
    ----------
    gti0 : iterable of the form ``[[gti0_0, gti0_1], [gti1_0, gti1_1], ...]``
    gti1 : iterable of the form ``[[gti0_0, gti0_1], [gti1_0, gti1_1], ...]``
        The two lists of GTIs to be crossed.

    Returns
    -------
    gtis : ``[[gti0_0, gti0_1], [gti1_0, gti1_1], ...]``
        The newly created GTIs

    See Also
    --------
    cross_gtis : From multiple GTI lists, extract common intervals *EXACTLY*

    Examples
    --------
    >>> gti1 = np.array([[1, 2]])
    >>> gti2 = np.array([[1, 2]])
    >>> newgti = cross_two_gtis(gti1, gti2)
    >>> np.all(newgti == [[1, 2]])
    True
    >>> gti1 = np.array([[1, 4]])
    >>> gti2 = np.array([[1, 2], [2, 4]])
    >>> newgti = cross_two_gtis(gti1, gti2)
    >>> np.all(newgti == [[1, 4]])
    True
    """
    gti0 = join_equal_gti_boundaries(np.asarray(gti0))
    gti1 = join_equal_gti_boundaries(np.asarray(gti1))
#     # Check GTIs
#     check_gtis(gti0)
#     check_gtis(gti1)

    gti0_start = gti0[:, 0]
    gti0_end = gti0[:, 1]
    gti1_start = gti1[:, 0]
    gti1_end = gti1[:, 1]

    # Create a list that references to the two start and end series
    gti_start = [gti0_start, gti1_start]
    gti_end = [gti0_end, gti1_end]

    # Concatenate the series, while keeping track of the correct origin of
    # each start and end time
    gti0_tag = np.array([0 for g in gti0_start], dtype=bool)
    gti1_tag = np.array([1 for g in gti1_start], dtype=bool)
    conc_start = np.concatenate((gti0_start, gti1_start))
    conc_end = np.concatenate((gti0_end, gti1_end))
    conc_tag = np.concatenate((gti0_tag, gti1_tag))

    # Put in time order
    order = np.argsort(conc_end)
    conc_start = conc_start[order]
    conc_end = conc_end[order]
    conc_tag = conc_tag[order]

    last_end = conc_start[0] - 1
    final_gti = []
    for ie, e in enumerate(conc_end):
        # Is this ending in series 0 or 1?
        this_series = int(conc_tag[ie])
        other_series = int(this_series == 0)

        # Check that this closes intervals in both series.
        # 1. Check that there is an opening in both series 0 and 1 lower than e
        try:
            st_pos = \
                np.argmax(gti_start[this_series][gti_start[this_series] < e])
            so_pos = \
                np.argmax(gti_start[other_series][gti_start[other_series] < e])
            st = gti_start[this_series][st_pos]
            so = gti_start[other_series][so_pos]

            s = np.max([st, so])
        except:  # pragma: no cover
            continue

        # If this start is inside the last interval (It can happen for equal
        # GTI start times between the two series), then skip!
        if s <= last_end:
            continue
        # 2. Check that there is no closing before e in the "other series",
        # from intervals starting either after s, or starting and ending
        # between the last closed interval and this one
        cond1 = (gti_end[other_series] > s) * (gti_end[other_series] < e)
        cond2 = gti_end[other_series][so_pos] < s
        condition = np.any(np.logical_or(cond1, cond2))
        # Well, if none of the conditions at point 2 apply, then you can
        # create the new gti!
        if not condition:
            final_gti.append([s, e])
            last_end = e

    return np.array(final_gti)


def get_visibility_windows(observation_start : float, observation_end : float,
                           orbital_period : float = 96. * 60,
                           exposure_per_orbit : float = 35. * 60,
                           phase_start : float = 0.):
    """Observing windows of a given target over multiple orbits.

    All quantities are defined in seconds.
    This function is unit-agnostic.

    Parameters
    ----------
    observation_start : float
        Observation start in seconds
    observation_end : float
        Observation end in seconds

    Other parameters
    ----------------
    orbital_period : float, default 96 mins
        Orbital period of the satellite, in seconds
    exposure_per_orbit : float, default 35 mins
        Time spent on a given target during an orbit, in seconds
    phase_start : float, default 0
        Orbital phase at which a target is observed


    Examples
    --------
    >>> ow = get_visibility_windows(0, 5760 * 2)
    >>> np.allclose(ow, [[0, 2100], [5760, 7860]])
    True
    """
    tstart = observation_start + orbital_period * phase_start
    start_times = np.arange(tstart, observation_end + orbital_period, orbital_period)
    end_times = start_times + exposure_per_orbit
    obs_windows = np.array(list(zip(start_times, end_times)))
    good = (start_times < observation_end)
    obs_windows[end_times > observation_end] = observation_end

    return obs_windows[good]


def calculate_flux(time : float, flux : float):
    """This is unit-agnostic, by choice."""
    dt = np.mean(np.diff(time))
    return np.sum(flux * dt) / (time[-1] - time[0])


def calculate_lightcurve_from_model(model_time, model_lc, observing_windows=None,
                                    visibility_windows=None,
                                    exposure_length=300 * u.s, **kwargs):
    """Calculate a light curve from the model lightcurve in ct/s.

    Parameters
    ----------
    model_time : ``astropy.units.s``
        Times at which the model is calculated
    model_lc : any ``astropy.units`` object expressing flux
        Values of the model

    Other parameters
    ----------------
    visibility_windows : [[start0, end0], [start1, end1], ...], ``astropy.units.s``, default None
        Visibility windows in seconds
    observing_windows : [[start0, end0], [start1, end1], ...], ``astropy.units.s``, default None
        Observing times in seconds
    exposure_length : ``astropy.units.s``, default 300 s
        Time for each exposure
    **kwargs :
        Additional keyword arguments to be passed to
        ``get_observing_windows``
    """
    import collections
    observing_windows = np.array([[model_time[0].to(u.s).value, model_time[-1].to(u.s).value]]) * u.s \
        if observing_windows is None else observing_windows

    if visibility_windows is None:
        visibility_windows = \
            get_visibility_windows(observing_windows.min().to(u.s).value,
                                   observing_windows.max().to(u.s).value, **kwargs) * u.s

    observing_windows = cross_two_gtis(observing_windows, visibility_windows) * u.s

    interpolated_lc = interp1d(model_time.to(u.s).value,
                               model_lc.value, fill_value=0, bounds_error=False)
    times = []
    lc = []
    for ow in observing_windows:
        expo_times = np.arange(ow[0].value, ow[1].value, exposure_length.value) * u.s
        for t in expo_times:
            times.append((t + exposure_length / 2).to(u.s).value)
            fine_times = np.linspace(t.value, (t + exposure_length).value, 10)
            fine_model = interpolated_lc(fine_times)
            flux = calculate_flux(fine_times * u.s, fine_model)
            lc.append(flux)

    result_table = QTable()
    result_table['time'] = np.array(times) * u.s
    result_table['Light curve'] = np.array(lc) * model_lc.unit

    return result_table


def calculate_snr(duet, band_fluence,
                  texp=300*u.s, read_noise=3. * (2 ** 0.5),
                  background=0):
    """Calculate the signal to noise ratio of from photon fluxes.

    Parameters
    ----------
    duet : a ``astroduet.config.Telescope`` instance
    band_fluence : array of floats
        Series of photon flux measurements, rescaled at 10 pc

    Other parameters
    ----------------
    background : float
        Background level
    texp : float
        Exposure time
    read_noise : float, default 3\sqrt(2)
        Readout noise
    """
    neff = get_neff(duet.psf_size, duet.pixel)
    efficiency = (duet.eff_epd / duet.EPD)**2
    area = np.pi * duet.EPD**2

    band_rate = duet.trans_eff * efficiency * area * band_fluence
    lc_snr = calc_snr(texp, band_rate, background,
                      read_noise, neff)
    return lc_snr


def get_lightcurve(input_lc_file, distance=10*u.pc, observing_windows=None,
                   duet=None, **kwargs):
    """Get a realistic light curve from a given theoretical light curve.

    Parameters
    ----------
    input_lc_file : str
        Light curve file containing photon fluxes

    Other parameters
    ----------------
    distance : ``astropy.units.pc``
        Distance of the SN event
    observing_windows : [[start0, end0], [start1, end1], ...], ``astropy.units.s``, default None
        Observing times in seconds
    duet : ``astroduet.config.Telescope`` object
        The telescope config to be used. Default config if None.
    """
    if duet is None:
        duet = Telescope()

    print('Effective PSF size {}'.format(duet.psf_size))
    # Get the number of effective background pixels
    neff = get_neff(duet.psf_size, duet.pixel)
    print('Number of effective bgd pixels: {}'.format(neff))
    print()

    bands = [duet.bandpass1, duet.bandpass2]

    fname, ext = os.path.splitext(input_lc_file)
    if ext == '.asc':
        model_lc_table = QTable()
        _mlt = Table.read(input_lc_file, format='ascii')
        model_lc_table['time'] = _mlt['time'] * u.s
        model_lc_table['photflux_D1'] = _mlt['photonflux_D1'] * (1 / u.s)
        model_lc_table['photflux_D2'] = _mlt['photonflux_D2'] * (1 / u.s)
    else:
        model_lc_table = QTable.read(input_lc_file)
    result_table = QTable()

    background = background_pixel_rate(duet, low_zodi=True)

    for duet_no in range(1, 3):
        duet_label = f'D{duet_no}'

        table_photflux = \
            calculate_lightcurve_from_model(
                model_lc_table['time'],
                model_lc_table[f'photflux_{duet_label}'],
                exposure_length=300 * u.s,
                observing_windows=observing_windows,
                **kwargs)

        if duet_no == 1:
            result_table['time'] = table_photflux['time']

        distance_conversion = (10 * u.pc / distance.to(u.pc)) ** 2
        
        result_table[f'photflux_{duet_label}'] = \
            table_photflux['Light curve'] * distance_conversion
            
        band_fluence = \
            table_photflux['Light curve'] * distance_conversion
            
        result_table[f'snr_{duet_label}'] = \
            calculate_snr(duet, band_fluence,
                          background=background[duet_no - 1])

        band = bands[duet_no - 1]
        flux = \
            table_photflux['Light curve'] * c.h * c.c / np.mean(band)

        flux_density = flux / (band[1] - band[0]) * distance_conversion
        abmag = flux_density.to(
            u.ABmag, equivalencies=u.spectral_density(np.mean(band)))

        abmag_err = sigerr(abmag)

        result_table[f'ABmag_{duet_label}'] = abmag
        result_table[f'ABmag_{duet_label}_err'] = abmag_err

    return result_table


def lightcurve_through_image(lightcurve, exposure,
                             frame=np.array([30, 30]),
                             final_resolution=None,
                             duet=None,
                             debug=False):
    """Transform a theoretical light curve into a flux measurement.

    1. Take the values of a light curve, optionally rebin it to a new time
    resolution.
    2. Then, create an image with a point source corresponding to each flux
    measurement, and calculate the flux from the image with ``daophot``.
    3. Return the ''realistic'' light curve

    Parameters
    ----------
    lightcurve : ``astropy.table.Table``
        The lightcurve has to contain the columns 'time', 'photflux_D1', and
        'photflux_D2'. Photon fluxes are in counts/s.
    exposure : ``astropy.units.Quantity``
        Exposure time used for the light curve

    Other parameters
    ----------------
    frame : [N, M]
        Number of pixel along x and y axis
    final_resolution : ``astropy.units.Quantity``, default None
        Rebin the light curve to this time resolution before creating the light
         curve. Must be > exposure
    duet : ``astroduet.config.Telescope``
        If None, a default one is created

    Returns
    -------
    lightcurve : ``astropy.table.Table``
        A light curve, rebinned to ``final_resolution``, and with four new
        columns: 'photflux_D1_fit', 'photflux_D1_fiterr', 'photflux_D2_fit',
        and 'photflux_D2_fiterr', containing the flux measurements from the
        intermediate images and their errorbars.
    """
    from astropy.table import Table
    lightcurve = copy.deepcopy(lightcurve)
    with suppress_stdout():
        if duet is None:
            duet = Telescope()

    good = (lightcurve['photflux_D1'] > 0) & (lightcurve['photflux_D2'] > 0)
    lightcurve = lightcurve[good]
    lightcurve['nbin'] = 1
    if final_resolution is not None:
        new_lightcurve = QTable()
        plain_lc = Table(lightcurve)
        time_bin = (plain_lc['time'] // final_resolution).to("").value
        lc_group = plain_lc.group_by(time_bin)
        plain_lc = lc_group.groups.aggregate(np.sum)
        for col in 'time,photflux_D1,photflux_D2'.split(','):
            new_lightcurve[col] = plain_lc[col] / plain_lc['nbin']
        new_lightcurve['nbin'] = plain_lc['nbin']
        lightcurve = new_lightcurve
    else:
        final_resolution = exposure


    with suppress_stdout():
        [bgd_band1, bgd_band2] = background_pixel_rate(duet, low_zodi=True,
                                                       diag=True)

    psf_fwhm_pix = duet.psf_fwhm / duet.pixel

    read_noise = duet.read_noise

    lightcurve['photflux_D1_fit'] = 0
    lightcurve['photflux_D1_fiterr'] = 0
    lightcurve['photflux_D2_fit'] = 0
    lightcurve['photflux_D2_fiterr'] = 0

    # Directory for debugging purposes
    rand = np.random.randint(0, 99999999)
    
    debugdir = f'debug_imgs_{final_resolution.to(u.s).value}s_{rand}'

    if debug:
        mkdir_p(debugdir)

    for i, row in enumerate(tqdm(lightcurve)):
        time = row['time']
        if row['photflux_D1'] == 0 or row['photflux_D2'] == 0:
            continue
        fl1 = duet.trans_eff * duet.eff_area * row['photflux_D1']
        fl2 = duet.trans_eff * duet.eff_area * row['photflux_D2']
        nave = row['nbin']
        with suppress_stdout():
            image1 = construct_image(frame, exposure * nave, read_noise,
                                     source=fl1,
                                     sky_rate=bgd_band1)
        image_rate1 = image1 / (exposure.value * nave)
        # star_tbl = Table(data=[[14], [14]], names=['x', 'y'])
        star_tbl, bkg_image, threshold = find(image_rate1, psf_fwhm_pix.value,
                                              method='daophot')
        if len(star_tbl) < 1:
            continue
        star_tbl.sort('flux')
        star_tbl = star_tbl[-1:]['x', 'y']

        with suppress_stdout():
            result1, _ = run_daophot(image_rate1, threshold,
                                     star_tbl, niters=1)
        fl1_fit, fl1_fite = result1['flux_fit'], result1['flux_unc']

        with suppress_stdout():
            image2 = construct_image(frame, exposure * nave, read_noise,
                                     source=fl2,
                                     sky_rate=bgd_band2)
        image_rate2 = image2 / (exposure.value * nave)
        star_tbl, bkg_image, threshold = find(image_rate2, psf_fwhm_pix.value,
                                              method='daophot')
        if len(star_tbl) < 1:
            continue
        star_tbl.sort('flux')
        star_tbl = star_tbl[-1:]['x', 'y']
        with suppress_stdout():
            result2, _ = run_daophot(image_rate2, threshold,
                                     star_tbl, niters=1)
        fl2_fit, fl2_fite = result2['flux_fit'], result2['flux_unc']

        lightcurve['photflux_D1_fit'][i] = fl1_fit
        lightcurve['photflux_D1_fiterr'][i] = fl1_fite
        lightcurve['photflux_D2_fit'][i] = fl2_fit
        lightcurve['photflux_D2_fiterr'][i] = fl2_fite
        if debug:
            pickle.dump({'imgD1': image_rate1, 'imgD2': image_rate2},
                        open(os.path.join(debugdir,
                                          f'images_{time.to(u.s).value}.p'),
                             'wb'))

    return lightcurve
