#!/usr/bin/env python
#-----------------------------------------------------------------------------
# Title      : pysmurf tune module - SmurfTuneMixin class
#-----------------------------------------------------------------------------
# File       : pysmurf/tune/smurf_tune.py
# Created    : 2018-08-31
#-----------------------------------------------------------------------------
# This file is part of the pysmurf software package. It is subject to
# the license terms in the LICENSE.txt file found in the top-level directory
# of this distribution and at:
#    https://confluence.slac.stanford.edu/display/ppareg/LICENSE.html.
# No part of the pysmurf software package, including this file, may be
# copied, modified, propagated, or distributed except according to the terms
# contained in the LICENSE.txt file.
#-----------------------------------------------------------------------------
import numpy as np
import os
import glob
import time
from pysmurf.client.base import SmurfBase
import scipy.signal as signal
from collections import Counter
from ..util import tools
from pysmurf.client.command.sync_group import SyncGroup as SyncGroup
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

class SmurfTuneMixin(SmurfBase):
    """
    This contains all the tuning scripts
    """

    def tune(self, load_tune=True, tune_file=None, last_tune=False,
             retune=False, f_min=.02, f_max=.3, df_max=.03,
             fraction_full_scale=None, make_plot=False,
             save_plot=True, show_plot=False,
             new_master_assignment=False, track_and_check=True):
        """
        This runs a tuning, does tracking setup, and prunes bad
        channels using check lock. When this is done, we should
        be ready to take data.
        Opt Args:
        ---------
        load_tune (bool): Whether to load in a tuning file. If False, will
            do a full tuning. This will be very slow (~ 1 hour)
        tune_file (str): The tuning file to load in. Default is None. If
            tune_file is None and last_tune is False, this will load the
            default tune file defined in exp.cfg.
        last_tune (bool): Whether to load the most recent tuning file. Default
            is False.
        retune (bool): Whether to re-run tune_band_serial to refind peaks and
            eta params. This will take about 5 minutes. Default is False.
        f_min (float): The minimum frequency swing allowable for check_lock.
        f_max (float): The maximum frequency swing allowable for check_lock.
        df_max (float): The maximum df stddev allowable for check_lock.
        fraction_full_scale (float): The fraction (between 0-1) to set the
            flux ramp amplitude.
        make_plot (bool): Whether to make a plot. Default is False.
        save_plot (bool): If making plots, whether to save them. Default is True.
        show_plot (bool): Whether to display the plots to screen. Default is False.
        track_and_check (bool): Whether or not after tuning to run
            track and check.  Default is True.
        new_master_assignment (bool): Whether to make a new master assignment
            which forces resonators at a given frequency to a given channel.
        """
        bands = self.config.get('init').get('bands')
        tune_cfg = self.config.get('tune_band')

        # Load fraction_full_scale from file if not given
        if fraction_full_scale is None:
            fraction_full_scale = tune_cfg.get('fraction_full_scale')

        if load_tune:
            if last_tune:
                tune_file = self.last_tune()
                self.log('Last tune is : {}'.format(tune_file))
            elif tune_file is None:
                tune_file = tune_cfg.get('default_tune')
                self.log('Loading default tune file: {}'.format(tune_file))
            self.load_tune(tune_file)

        # Runs find_freq and setup_notches. This takes forever.
        else:
            cfg = self.config.get('init')
            for b in bands:
                drive = cfg.get('band_{}'.format(b)).get('amplitude_scale')
                self.find_freq(b,
                    drive_power=drive)
                self.setup_notches(b, drive=drive,
                    new_master_assignment=new_master_assignment)

        # Runs tune_band_serial to re-estimate eta params
        if retune:
            for b in bands:
                self.log('Running tune band serial on band {}'.format(b))
                self.tune_band_serial(b, from_old_tune=load_tune,
                    old_tune=tune_file, make_plot=make_plot,
                    show_plot=show_plot, save_plot=save_plot,
                    new_master_assignment=new_master_assignment)

        # Starts tracking and runs check_lock to prune bad resonators
        if track_and_check:
            for b in bands:
                self.log('Tracking and checking band {}'.format(b))
                self.track_and_check(b, fraction_full_scale=fraction_full_scale,
                    f_min=f_min, f_max=f_max, df_max=df_max, make_plot=make_plot,
                    save_plot=save_plot, show_plot=show_plot)


    def tune_band(self, band, freq=None, resp=None, n_samples=2**19,
            make_plot=False, show_plot=False, plot_chans=[], save_plot=True,
            save_data=True,  make_subband_plot=False, subband=None, n_scan=5,
            subband_plot_with_slow=False, drive=None, grad_cut=.05, freq_min=-2.5E8,
            freq_max=2.5E8, amp_cut=.5, use_slow_eta=False):
        """
        This does the full_band_resp, which takes the raw resonance data.
        It then finds the where the resonances are. Using the resonance
        locations, it calculates the eta parameters.
        Args:
        -----
        band (int): The band to tune
        Opt Args:
        ---------
        freq (float array): The frequency information. If both freq and resp
            are not None, it will skip full_band_resp.
        resp (float array): The response information. If both freq and resp
            are not None, it will skip full_band_resp.
        n_samples (int): The number of samples to take in full_band_resp.
            Default is 2^19.
        make_plot (bool): Whether to make plots. This is slow, so if you want
            to tune quickly, set to False. Default True.
        plot_chans (list): if making plots, which channels to plot. If empty,
            will just plot all of them
        save_plot (bool): Whether to save the plot. If True, it will close the
            plots before they are shown. If False, plots will be brought to the
            screen.
        save_data (bool): If True, saves the data to disk.
        grad_cut (float): The value of the gradient of phase to look for
            resonances. Default is .05
        amp_cut (float): The distance from the median value to decide whether
            there is a resonance. Default is .25.
        freq_min (float): The minimum frequency relative to the center of
            the band to look for resonances. Units of Hz. Defaults is -2.5E8
        freq_max (float): The maximum frequency relative to the center of
            the band to look for resonances. Units of Hz. Defaults is 2.5E8
        Returns:
        --------
        res (dict): A dictionary with resonance frequency, eta, eta_phase,
            R^2, and amplitude.
        """
        timestamp = self.get_timestamp()

        if make_plot and save_plot:
            plt.ioff()

        if freq is None or resp is None:
            self.band_off(band)
            self.flux_ramp_off()
            self.log('Running full band resp')

            # Inject high amplitude noise with known waveform, measure it, and
            # then find resonators and etaParameters from cross-correlation.
            freq, resp = self.full_band_resp(band, n_samples=n_samples,
                make_plot=make_plot, save_data=save_data, timestamp=timestamp,
                n_scan=n_scan, show_plot=show_plot)


        # Find peaks
        peaks = self.find_peak(freq, resp, rolling_med=True, band=band,
            make_plot=make_plot, show_plot=show_plot, window=5000,
            save_plot=save_plot, grad_cut=grad_cut, freq_min=freq_min,
            freq_max=freq_max, amp_cut=amp_cut,
            make_subband_plot=make_subband_plot, timestamp=timestamp,
            subband_plot_with_slow=subband_plot_with_slow, pad=50, min_gap=50)

        # Eta scans
        band_center_mhz = self.get_band_center_mhz(band)
        resonances = {}
        for i, p in enumerate(peaks):
            eta, eta_scaled, eta_phase_deg, r2, eta_mag, latency, Q= \
                self.eta_fit(band, freq, resp, p, 50E3, make_plot=False,
                plot_chans=plot_chans, save_plot=save_plot, res_num=i,
                band=band, timestamp=timestamp, use_slow_eta=use_slow_eta)

            # Fill the resonances dict
            resonances[i] = {
                'freq': p*1.0E-6 + band_center_mhz,
                'eta': eta,
                'eta_scaled': eta_scaled,
                'eta_phase': eta_phase_deg,
                'r2': r2,
                'eta_mag': eta_mag,
                'latency': latency,
                'Q': Q
            }

        if save_data:
            self.log('Saving resonances to {}'.format(self.output_dir))
            path = os.path.join(self.output_dir,
                                '{}_b{}_resonances'.format(timestamp, band))
            np.save(path, resonances)
            self.pub.register_file(path, 'resonances', format='npyt')

        # Assign resonances to channels
        self.log('Assigning channels')
        f = [resonances[k]['freq'] for k in resonances.keys()]
        subbands, channels, offsets = self.assign_channels(f, band=band)

        for i, k in enumerate(resonances.keys()):
            resonances[k].update({'subband': subbands[i]})
            resonances[k].update({'channel': channels[i]})
            resonances[k].update({'offset': offsets[i]})

        self.freq_resp[band]['resonances'] = resonances
        if drive is None:
            drive = self.config.get('init').get('band_{}'.format(band)).get('amplitude_scale')

        # Add tone amplitude to tuning dictionary
        self.freq_resp[band]['drive'] = drive

        # Save the data
        self.save_tune()

        self.relock(band)
        self.log('Done tuning')

        return resonances

    def tune_band_serial(self, band, n_samples=2**19,
            make_plot=False, save_plot=True, save_data=True, show_plot=False,
            make_subband_plot=False, subband=None, n_scan=5,
            subband_plot_with_slow=False, window=5000, rolling_med=True,
            grad_cut=.03, freq_min=-2.5E8, freq_max=2.5E8, amp_cut=.25,
            del_f=.005, drive=None, new_master_assignment=False, from_old_tune=False,
            old_tune=None, pad=50, min_gap=50):
        """
        Tunes band using serial_gradient_descent and then serial_eta_scan.
        This requires an initial guess, which this function gets by either
        loading an old tune or by using the full_band_resp.  This takes about 3
        minutes per band if there are about 150 resonators.
        This saves the results to the freq_resp dictionary.
        Args:
        -----
        band (int): The band the tune
        Opt Args:
        ---------
        from_old_tune (bool): Whether to use an old tuning file. This
            will load a tuning file and use its peak frequencies as
            a starting point for seria_gradient_descent.
        old_tune (str): The full path to the tuning file.
        new_master_assignment (bool): Whether to overwrite the previous
            master_assignment list. Default is False.
        make_plot (bool): Whether to make plots. Default is False.
        save_plot (bool): If make_make plot is True, whether to save
            the plots. Default is True.
        show_plot (bool): If make_plot is True, whether to display
            the plots to screen.
        """
        timestamp = self.get_timestamp()
        center_freq = self.get_band_center_mhz(band)

        self.flux_ramp_off()  # flux ramping messes up eta params

        freq=None
        resp=None
        if from_old_tune:
            if old_tune is None:
                self.log('Using default tuning file')
                old_tune = self.config.get('tune_band').get('default_tune')
            self.load_tune(old_tune,band=band)

            resonances = np.copy(self.freq_resp[band]['resonances']).item()

            if new_master_assignment:
                f = np.array([resonances[k]['freq'] for k in resonances.keys()])
                # f += self.get_band_center_mhz(band)
                subbands, channels, offsets = self.assign_channels(f, band=band,
                    as_offset=False, new_master_assignment=new_master_assignment)

                for i, k in enumerate(resonances.keys()):
                    resonances[k].update({'subband': subbands[i]})
                    resonances[k].update({'channel': channels[i]})
                    resonances[k].update({'offset': offsets[i]})
                self.freq_resp[band]['resonances'] = resonances

        else:
            # Inject high amplitude noise with known waveform, measure it, and
            # then find resonators and etaParameters from cross-correlation.
            old_att = self.get_att_uc(band)
            self.set_att_uc(band, 0, wait_after=.5, write_log=True)
            self.get_att_uc(band, write_log=True)
            freq, resp = self.full_band_resp(band, n_samples=n_samples,
                                         make_plot=make_plot, save_data=save_data,
                                         show_plot=False, timestamp=timestamp,
                                         n_scan=n_scan)
            self.set_att_uc(band, old_att, write_log=True)

            # Find peaks
            peaks = self.find_peak(freq, resp, rolling_med=rolling_med,
                window=window, band=band, make_plot=make_plot,
                save_plot=save_plot,  show_plot=show_plot, grad_cut=grad_cut,
                freq_min=freq_min, freq_max=freq_max, amp_cut=amp_cut,
                make_subband_plot=make_subband_plot, timestamp=timestamp,
                subband_plot_with_slow=subband_plot_with_slow, pad=pad,
                min_gap=min_gap)

            resonances = {}
            for i, p in enumerate(peaks):
                resonances[i] = {
                    'freq': p*1.0E-6 + center_freq,  # in MHz
                    'r2' : 0,
                    'Q' : 1,
                    'eta_phase' : 1 , # Fill etas with arbitrary values for now
                    'eta_scaled' : 1,
                    'eta_mag' : 0,
                    'eta' : 0 + 0.j
                }

            # Assign resonances to channels
            self.log('Assigning channels')
            f = np.array([resonances[k]['freq'] for k in resonances.keys()])
            subbands, channels, offsets = self.assign_channels(f, band=band,
                as_offset=False, new_master_assignment=new_master_assignment)

            for i, k in enumerate(resonances.keys()):
                resonances[k].update({'subband': subbands[i]})
                resonances[k].update({'channel': channels[i]})
                resonances[k].update({'offset': offsets[i]})
                self.freq_resp[band]['resonances'] = resonances

        if drive is None:
            drive = \
                self.config.get('init')['band_{}'.format(band)]['amplitude_scale']
        self.freq_resp[band]['drive'] = drive
        self.freq_resp[band]['full_band_resp'] = {}
        if freq is not None:
            self.freq_resp[band]['full_band_resp']['freq'] = freq * 1.0E-6 + center_freq
        if resp is not None:
            self.freq_resp[band]['full_band_resp']['resp'] = resp
        self.freq_resp[band]['timestamp'] = timestamp


        # Set the resonator frequencies without eta params
        self.relock(band, drive=drive)

        # Find the resonator minima
        self.log('Finding resonator minima...')
        self.run_serial_gradient_descent(band, timeout=1200)
        #self.run_serial_min_search(band)

        # Calculate the eta params
        self.log('Calculating eta parameters...')
        self.run_serial_eta_scan(band, timeout=1200)

        # Read back new eta parameters and populate freq_resp
        subband_half_width = self.get_digitizer_frequency_mhz(band)/\
            self.get_number_sub_bands(band)
        eta_phase = self.get_eta_phase_array(band)
        eta_scaled = self.get_eta_mag_array(band)
        eta_mag = eta_scaled * subband_half_width
        eta = eta_mag * np.cos(np.deg2rad(eta_phase)) + \
            1.j * np.sin(np.deg2rad(eta_phase))

        chs = self.get_eta_scan_result_channel(band)

        chs = self.get_eta_scan_result_channel(band)

        for i, ch in enumerate(chs):
            if ch != -1:
                resonances[i]['eta_phase'] = eta_phase[ch]
                resonances[i]['eta_scaled'] = eta_scaled[ch]
                resonances[i]['eta_mag'] = eta_mag[ch]
                resonances[i]['eta'] = eta[ch]

        self.freq_resp[band]['resonances'] = resonances

        self.save_tune()

        self.log('Done with serial tuning')


    def plot_tune_summary(self, band, eta_scan=False, show_plot=False,
            save_plot=True, eta_width=.3, channels=None,
            plot_summary=True, plotname_append=''):
        """
        Plots summary of tuning. Requires self.freq_resp to be filled.
        In other words, you must run find_freq and setup_notches
        before calling this function. Saves the plot to plot_dir.
        This will also make individual eta plots as well if {eta_scan} is True.
        The eta scan plots are slow because there are many of them.

        Args:
        -----
        band (int): The band number to plot

        Opt Args:
        ---------
        eta_scan (bool) : Whether to also plot individual eta scans.
           Warning this is slow. Default is False.
        show_plot (bool) : Whether to display the plot. Default is False.
        save_plot (bool) : Whether to save the plot. Default is True.
        eta_width (float) : The width to plot in MHz.
        channels (int list) : Which channels to plot.  Default is None,
                             which plots all available channels.
        plot_summary (bool) : Plot summary.
        plotname_append (string): Appended to the default plot filename. Default
            is ''.
        """
        if show_plot:
            plt.ion()
        else:
            plt.ioff()

        timestamp = self.get_timestamp()

        if plot_summary:
            fig, ax = plt.subplots(2,2, figsize=(10,6))

            # Subband
            sb = self.get_eta_scan_result_subband(band)
            ch = self.get_eta_scan_result_channel(band)
            idx = np.where(ch!=-1)  # ignore unassigned channels
            sb = sb[idx]
            c = Counter(sb)
            y = np.array([c[i] for i in np.arange(128)])
            ax[0,0].plot(np.arange(128), y, '.', color='k')
            for i in np.arange(0, 128, 16):
                ax[0,0].axvspan(i-.5, i+7.5, color='k', alpha=.2)
            ax[0,0].set_ylim((-.2, np.max(y)+1.2))
            ax[0,0].set_yticks(np.arange(0,np.max(y)+.1))
            ax[0,0].set_xlim((0, 128))
            ax[0,0].set_xlabel('Subband')
            ax[0,0].set_ylabel('# Res')
            ax[0,0].text(.02, .92, 'Total: {}'.format(len(sb)),
                         fontsize=10, transform=ax[0,0].transAxes)

            # Eta stuff
            eta = self.get_eta_scan_result_eta(band)
            eta = eta[idx]
            f = self.get_eta_scan_result_freq(band)
            f = f[idx]

            ax[0,1].plot(f, np.real(eta), '.', label='Real')
            ax[0,1].plot(f, np.imag(eta), '.', label='Imag')
            ax[0,1].plot(f, np.abs(eta), '.', label='Abs', color='k')
            ax[0,1].legend(loc='lower right')
            bc = self.get_band_center_mhz(band)
            ax[0,1].set_xlim((bc-250, bc+250))
            ax[0,1].set_xlabel('Freq [MHz]')
            ax[0,1].set_ylabel('Eta')

            phase = np.rad2deg(np.angle(eta))
            ax[1,1].plot(f, phase, color='k')
            ax[1,1].set_xlim((bc-250, bc+250))
            ax[1,1].set_ylim((-180,180))
            ax[1,1].set_yticks(np.arange(-180, 180.1, 90))
            ax[1,1].set_xlabel('Freq [MHz]')
            ax[1,1].set_ylabel('Eta phase')

            fig.suptitle('Band {} {}'.format(band, timestamp))
            plt.subplots_adjust(left=.08, right=.95, top=.92, bottom=.08,
                                wspace=.21, hspace=.21)

            if save_plot:
                save_name = '{}_tune_summary{}.png'.format(timestamp,
                    plotname_append)
                path = os.path.join(self.plot_dir, save_name)
                plt.savefig(path, bbox_inches='tight')
                self.pub.register_file(path, 'tune', plot=True)
                if not show_plot:
                    plt.close()

        # Plot individual eta scan
        if eta_scan:
            keys = self.freq_resp[band]['resonances'].keys()
            n_keys = len(keys)
            # If using full band response as input
            if 'full_band_resp' in self.freq_resp[band]:
                freq = self.freq_resp[band]['full_band_resp']['freq']
                resp = self.freq_resp[band]['full_band_resp']['resp']
                for k in keys:
                    r = self.freq_resp[band]['resonances'][k]
                    channel=r['channel']
                    # If user provides a channel restriction list, only
                    # plot channels in that list.
                    if channel is not None and channel not in channels:
                        continue
                    center_freq = r['freq']
                    idx = np.logical_and(freq > center_freq - eta_width,
                        freq < center_freq + eta_width)

                    # Actually plot the data
                    self.plot_eta_fit(freq[idx], resp[idx],
                        eta_mag=r['eta_mag'], eta_phase_deg=r['eta_phase'],
                        band=band, res_num=k, timestamp=timestamp,
                        save_plot=save_plot, show_plot=show_plot,
                        peak_freq=center_freq, channel=channel, plotname_append=plotname_append)
            # This is for data from find_freq/setup_notches
            else:
                for k in keys:
                    r = self.freq_resp[band]['resonances'][k]
                    channel=r['channel']
                    # If user provides a channel restriction list, only
                    # plot channels in that list.
                    if channels is not None:
                        if channel not in channels:
                            continue
                        else:
                            self.log('Eta plot for channel {}'.format(channel))
                    else:
                        self.log('Eta plot {} of {}'.format(k+1, n_keys))
                        self.plot_eta_fit(r['freq_eta_scan'], r['resp_eta_scan'],
                            eta=r['eta'], eta_mag=r['eta_mag'],
                            eta_phase_deg=r['eta_phase'], band=band, res_num=k,
                            timestamp=timestamp, save_plot=save_plot,
                            show_plot=show_plot, peak_freq=r['freq'],
                            channel=channel, plotname_append=plotname_append)


    def full_band_resp(self, band, n_scan=1, n_samples=2**19, make_plot=False,
            save_plot=True, show_plot=False, save_data=False, timestamp=None,
            save_raw_data=False, correct_att=True, swap=False, hw_trigger=True,
            write_log=False, return_plot_path=False,
            check_if_adc_is_saturated=True):
        """
        Injects high amplitude noise with known waveform. The ADC measures it.
        The cross correlation contains the information about the resonances.
        Args:
        -----
        band (int): The band to sweep.

        Opt Args:
        ---------
        n_scan (int): The number of scans to take and average
        n_samples (int): The number of samples to take. Default 2^18.
        make_plot (bool): Whether the make plots. Default is False.
        show_plot (bool): Whether to show plots. Default False
        save_data (bool): Whether to save the data.
        save_raw_data (bool): Whether to save the raw ADC/DAC data. Default
            False.
        timestamp (str): The timestamp as a string. If None, loads the current
            timestamp.
        correct_att (bool): Correct the response for the attenuators. Default
            is True.
        swap (bool): Whether to reverse the data order of the ADC relative
            to the DAC. This solved an legacy problem. Default False.
        hw_trigger (bool): Whether to start the broadband noise file using
            the hardware trigger. Default True.
        return_plot_path (bool) : Whether to return the full path to the
            summary plot. Default False.
        check_if_adc_is_saturated (bool): Right after playing the
            noise file, checks if ADC for the requested band is
            saturated.  If it is saturated, gives up with an error.

        Returns:
        --------
        f (float array): The frequency information. Length n_samples/2
        resp (complex array): The response information. Length n_samples/2
        """
        if timestamp is None:
            timestamp = self.get_timestamp()

        resp = np.zeros((int(n_scan), int(n_samples/2)), dtype=complex)
        for n in np.arange(n_scan):
            bay = self.band_to_bay(band)
            # Default setup sets to 1
            self.set_trigger_hw_arm(bay, 0, write_log=write_log)

            self.set_noise_select(band, 1, wait_done=True, write_log=write_log)
            # if true, checks whether or not playing noise file saturates the ADC.
            #If ADC is saturated, throws an exception.
            if check_if_adc_is_saturated:
                adc_is_saturated = self.check_adc_saturation(band)
                if adc_is_saturated:
                    raise ValueError('Playing the noise file saturates the '+
                        f'ADC for band {band}.  Try increasing the DC '+
                        'attenuation for this band.')

            # Take read the ADC data
            try:
                adc = self.read_adc_data(band, n_samples, hw_trigger=hw_trigger,
                    save_data=False)
            except Exception:
                self.log('ADC read failed. Trying one more time', self.LOG_ERROR)
                adc = self.read_adc_data(band, n_samples, hw_trigger=hw_trigger,
                    save_data=False)
            time.sleep(.05)  # Need to wait, otherwise dac call interferes with adc

            try:
                dac = self.read_dac_data(band, n_samples, hw_trigger=hw_trigger)
            except BaseException:
                self.log('ADC read failed. Trying one more time', self.LOG_ERROR)
                dac = self.read_dac_data(band, n_samples, hw_trigger=hw_trigger,
                    save_data=False)
            time.sleep(.05)

            self.set_noise_select(band, 0, wait_done=True, write_log=write_log)

            # Account for the up and down converter attenuators
            if correct_att:
                att_uc = self.get_att_uc(band)
                att_dc = self.get_att_dc(band)
                self.log(f'UC (DAC) att: {att_uc}')
                self.log(f'DC (ADC) att: {att_dc}')
                if att_uc > 0:
                    scale = (10**(-att_uc/2/20))
                    self.log(f'UC attenuator > 0. Scaling by {scale:4.3f}')
                    dac *= scale
                if att_dc > 0:
                    scale = (10**(att_dc/2/20))
                    self.log(f'DC attenuator > 0. Scaling by {scale:4.3f}')
                    adc *= scale

            if save_raw_data:
                self.log('Saving raw data...', self.LOG_USER)

                path = os.path.join(self.output_dir, f'{timestamp}_adc')
                np.save(path, adc)
                self.pub.register_file(path, 'adc', format='npy')

                path = os.path.join(self.output_dir,f'{timestamp}_dac')
                np.save(path, dac)
                self.pub.register_file(path, 'dac', format='npy')

            # Swap frequency ordering of data of ADC relative to DAC
            if swap:
                adc = adc[::-1]

            # Take PSDs of ADC, DAC, and cross
            fs = self.get_digitizer_frequency_mhz() * 1.0E6
            f, p_dac = signal.welch(dac, fs=fs, nperseg=n_samples/2,
                                    return_onesided=True)
            f, p_adc = signal.welch(adc, fs=fs, nperseg=n_samples/2,
                                    return_onesided=True)
            f, p_cross = signal.csd(dac, adc, fs=fs, nperseg=n_samples/2,
                                    return_onesided=True)

            # Sort frequencies
            idx = np.argsort(f)
            f = f[idx]
            p_dac = p_dac[idx]
            p_adc = p_adc[idx]
            p_cross = p_cross[idx]

            resp[n] = p_cross / p_dac

        # Average over the multiple scans
        resp = np.mean(resp, axis=0)

        plot_path = None
        if make_plot:
            if show_plot:
                plt.ion()
            else:
                plt.ioff()

            fig, ax = plt.subplots(3, figsize=(5,8), sharex=True)
            f_plot = f / 1.0E6

            plot_idx = np.where(np.logical_and(f_plot>-250, f_plot<250))

            ax[0].semilogy(f_plot, p_dac)
            ax[0].set_ylabel('DAC')
            ax[1].semilogy(f_plot, p_adc)
            ax[1].set_ylabel('ADC')
            ax[2].semilogy(f_plot, np.abs(p_cross))
            ax[2].set_ylabel('Cross')
            ax[2].set_xlabel('Frequency [MHz]')
            ax[0].set_title(timestamp)

            if save_plot:
                path = os.path.join(self.plot_dir,
                    '{}_b{}_full_band_resp_raw.png'.format(timestamp, band))
                plt.savefig(path, bbox_inches='tight')
                self.pub.register_file(path, 'response', plot=True)
                plt.close()

            fig, ax = plt.subplots(1, figsize=(5.5, 3))

            # Log y-scale plot
            ax.plot(f_plot[plot_idx], np.log10(np.abs(resp[plot_idx])))
            ax.set_xlabel('Freq [MHz]')
            ax.set_ylabel('Response')
            ax.set_title(f'full_band_resp {timestamp}')
            plt.tight_layout()
            if save_plot:
                plot_path = os.path.join(self.plot_dir,
                    '{}_b{}_full_band_resp.png'.format(timestamp, band))

                plt.savefig(plot_path, bbox_inches='tight')
                self.pub.register_file(plot_path, 'response', plot=True)

            # Show/Close plots
            if show_plot:
                plt.show()
            else:
                plt.close()

        if save_data:
            save_name = timestamp + '_{}_full_band_resp.txt'

            path = os.path.join(self.output_dir, save_name.format('freq'))
            np.savetxt(path, f)
            self.pub.register_file(path, 'full_band_resp', format='txt')

            path = os.path.join(self.output_dir, save_name.format('real'))
            np.savetxt(path, np.real(resp))
            self.pub.register_file(path, 'full_band_resp', format='txt')

            path = os.path.join(self.output_dir, save_name.format('imag'))
            np.savetxt(path, np.imag(resp))
            self.pub.register_file(path, 'full_band_resp', format='txt')

        if return_plot_path:
            return f, resp, plot_path
        else:
            return f, resp


    def find_peak(self, freq, resp, rolling_med=True, window=5000,
            grad_cut=.5, amp_cut=.25, freq_min=-2.5E8, freq_max=2.5E8,
            make_plot=False, save_plot=True, plotname_append='', show_plot=False,
            band=None, subband=None, make_subband_plot=False,
            subband_plot_with_slow=False, timestamp=None, pad=50, min_gap=100,
            plot_title=None, grad_kernel_width=8):
        """
        Find the peaks within a given subband.

        Args:
        -----
        freq (float array): should be a single row of the broader freq
                            array, in Mhz.
        resp (complex array): complex response for just this subband

        Opt Args:
        ---------
        rolling_med (bool): whether to use a rolling median for the background
        window (int): number of samples to window together for rolling med
        grad_cut (float): The value of the gradient of phase to look for
            resonances. Default is .05
        amp_cut (float): The fractional distance from the median value to decide
            whether there is a resonance. Default is .25.
        freq_min (float): The minimum frequency relative to the center of
            the band to look for resonances. Units of Hz. Defaults is -2.5E8
        freq_max (float): The maximum frequency relative to the center of
            the band to look for resonances. Units of Hz. Defaults is 2.5E8
        make_plot (bool): Whether to make a plot. Default is False.
        make_subband_plot (bool): Whether to make a plot per subband. This is
            very slow. Default is False.
        save_plot (bool): Whether to save the plot to self.plot_dir. Default
            is True.
        plotname_append (string): Appended to the default plot filename.
            Default is ''.
        band (int): The band to take find the peaks in. Mainly for saving
            and plotting.
        timestamp (str): The timestamp. Mainly for saving and plotting
        pad (int): number of samples to pad on either side of a resonance search
            window
        min_gap (int): minimum number of samples between resonances
        grad_kernel_width (int) : The number of samples to take after a point
            to calculate the gradient of phase. Default is 8.

        Returns:
        -------_
        resonances (float array): The frequency of the resonances in the band
            in Hz.
        """
        if timestamp is None:
            timestamp = self.get_timestamp()

        # Break apart the data
        angle = np.unwrap(np.angle(resp))
        x = np.arange(len(angle))
        p1 = np.poly1d(np.polyfit(x, angle, 1))
        angle -= p1(x)
        grad = np.convolve(angle, np.repeat([1,-1], grad_kernel_width),
            mode='same')

        amp = np.abs(resp)

        grad_loc = np.array(grad > grad_cut)

        # Calculate the rolling median. This uses pandas.
        if rolling_med:
            import pandas as pd
            med_amp = pd.Series(amp).rolling(window=window, center=True,
                                             min_periods=1).median()
        else:
            med_amp = np.median(amp) * np.ones(len(amp))

        # Get the flagging
        starts, ends = self.find_flag_blocks(self.pad_flags(grad_loc,
            before_pad=pad, after_pad=pad, min_gap=min_gap))

        # Find the peaks locations
        peak = np.array([], dtype=int)
        for s, e in zip(starts, ends):
            if freq[s] > freq_min and freq[e] < freq_max:
                idx = np.ravel(np.where(amp[s:e] == np.min(amp[s:e])))[0]
                idx += s
                if 1-amp[idx]/med_amp[idx] > amp_cut:
                    peak = np.append(peak, idx)

        # Make summary plot
        if make_plot:
            if show_plot:
                plt.ion()
            else:
                plt.ioff()

            fig, ax = plt.subplots(2, figsize=(8,6), sharex=True)

            if band is not None:
                bandCenterMHz = self.get_band_center_mhz(band)
                plot_freq_mhz = freq+bandCenterMHz
            else:
                plot_freq_mhz = freq

            ax[0].plot(plot_freq_mhz, amp)
            ax[0].plot(plot_freq_mhz, med_amp)
            ax[0].plot(plot_freq_mhz[peak], amp[peak], 'kx')
            ax[1].plot(plot_freq_mhz, grad)

            ax[1].set_ylim(-2, 20)
            for s, e in zip(starts, ends):
                ax[0].axvspan(plot_freq_mhz[s], plot_freq_mhz[e], color='k',
                    alpha=.1)
                ax[1].axvspan(plot_freq_mhz[s], plot_freq_mhz[e], color='k',
                    alpha=.1)


            ax[0].set_ylabel('Amp.')
            ax[1].set_xlabel('Freq. [MHz]')

            title = timestamp
            if band is not None:
                title = title + f': band {band}, center = {bandCenterMHz:.1f} MHz'
            if subband is not None:
                title = title + f' subband {subband}'
            fig.suptitle(title)

            if save_plot:
                save_name = timestamp
                if band is not None:
                    save_name = save_name + '_b{}'.format(int(band))
                if subband is not None:
                    save_name = save_name + '_sb{}'.format(int(subband))
                save_name = save_name + '_find_freq' + plotname_append + '.png'
                path = os.path.join(self.plot_dir, save_name)
                plt.savefig(path, bbox_inches='tight', dpi=300)
                self.pub.register_file(path, 'find_freq', plot=True)
            if show_plot:
                plt.show()
            else:
                plt.close()

        # Make plot per subband
        if make_subband_plot:
            subbands, subband_freq = self.get_subband_centers(band,
                hardcode=True)  # remove hardcode mode
            plot_freq_mhz = freq
            plot_width = 5.5  # width of plotting in MHz
            width = (subband_freq[1] - subband_freq[0])

            for sb, sbf in zip(subbands, subband_freq):
                self.log('Making plot for subband {}'.format(sb))
                idx = np.logical_and(plot_freq_mhz > sbf - plot_width/2.,
                    plot_freq_mhz < sbf + plot_width/2.)
                if np.sum(idx) > 1:
                    f = plot_freq_mhz[idx]
                    p = angle[idx]
                    x = np.arange(len(p))
                    fp = np.polyfit(x, p, 1)
                    p = p - x*fp[0] - fp[1]

                    g = grad[idx]
                    a = amp[idx]
                    ma = med_amp[idx]

                    fig, ax = plt.subplots(2, sharex=True)
                    ax[0].plot(f, p, label='Phase')
                    ax[0].plot(f, g, label=r'$\Delta$ phase')
                    ax[1].plot(f, a, label='Amp')
                    ax[1].plot(f, ma, label='Median Amp')
                    for s, e in zip(starts, ends):
                        if (plot_freq_mhz[s] in f) or (plot_freq_mhz[e] in f):
                            ax[0].axvspan(plot_freq_mhz[s], plot_freq_mhz[e],
                                color='k', alpha=.1)
                            ax[1].axvspan(plot_freq_mhz[s], plot_freq_mhz[e],
                                color='k', alpha=.1)

                    for pp in peak:
                        if plot_freq_mhz[pp] > sbf - plot_width/2. and \
                                plot_freq_mhz[pp] < sbf + plot_width/2.:
                            ax[1].plot(plot_freq_mhz[pp], amp[pp], 'xk')

                    ax[0].legend(loc='upper right')
                    ax[1].legend(loc='upper right')

                    ax[0].axvline(sbf, color='k' ,linestyle=':', alpha=.4)
                    ax[1].axvline(sbf, color='k' ,linestyle=':', alpha=.4)
                    ax[0].axvline(sbf - width/2., color='k' ,linestyle='--',
                                  alpha=.4)
                    ax[0].axvline(sbf + width/2., color='k' ,linestyle='--',
                                  alpha=.4)
                    ax[1].axvline(sbf - width/2., color='k' ,linestyle='--',
                                  alpha=.4)
                    ax[1].axvline(sbf + width/2., color='k' ,linestyle='--',
                                  alpha=.4)

                    ax[1].set_xlim((sbf-plot_width/2., sbf+plot_width/2.))

                    ax[0].set_ylabel('[Rad]')
                    ax[1].set_xlabel('Freq [MHz]')
                    ax[1].set_ylabel('Amp')

                    ax[0].set_title('Band {} Subband {}'.format(band,
                                                                sb, sbf))

                    if subband_plot_with_slow:
                        ff = np.arange(-3, 3.1, .05)
                        rr, ii = self.eta_scan(band, sb, ff, 10, write_log=False)
                        dd = rr + 1.j*ii
                        sbc = self.get_subband_centers(band)
                        ax[1].plot(ff+sbc[1][sb], np.abs(dd)/2.5E6)

                    if save_plot:
                        pna = plotname_append
                        save_name = f'{timestamp}_find_freq_b{band}_sb{sb:03}{pna}.png'
                        os.path.join(self.plot_dir, save_name)
                        plt.savefig(path, bbox_inches='tight')
                        self.pub.register_file(path, 'find_freq', plot=True)
                        plt.close()
                else:
                    self.log('No data for subband {}'.format(sb))

        return freq[peak]

    def find_flag_blocks(self, flag, minimum=None, min_gap=None):
        """
        Find blocks of adjacent points in a boolean array with the same value.

        Args:
        -----
        flag : bool, array_like
            The array in which to find blocks

        Opt Args:
        ---------
        minimum : int (optional)
            The minimum length of block to return. Discards shorter blocks
        min_gap : int (optional)
            The minimum gap between flag blocks. Fills in gaps smaller.
        Returns
        -------
        starts, ends : int arrays
            The start and end indices for each block.
            NOTE: the end index is the last index in the block. Add 1 for
            slicing, where the upper limit should be after the block
        """
        if min_gap is not None:
            _flag = self.pad_flags(np.asarray(flag, dtype=bool),
                min_gap=min_gap).astype(np.int8)
        else:
            _flag = np.asarray(flag).astype(int)

        marks = np.diff(_flag)
        start = np.where(marks == 1)[0]+1
        if _flag[0]:
            start = np.concatenate([[0],start])
        end = np.where(marks == -1)[0]
        if _flag[-1]:
            end = np.concatenate([end,[len(_flag)-1]])

        if minimum is not None:
            inds = np.where(end - start + 1 > minimum)[0]
            return start[inds],end[inds]
        else:
            return start,end


    def pad_flags(self, f, before_pad=0, after_pad=0, min_gap=0, min_length=0):
        """
        Adds and combines flagging.

        Args:
        -----
        f (bool array): The flag array to pad
        Opt Args:
        ---------
        before_pad (int): The number of samples to pad before a flag
        after_pad (int); The number of samples to pad after a flag
        min_gap (int): The smallest allowable gap. If bigger, it combines.
        min_length (int): The smallest length a pad can be.
        Ret:
        ----
        pad_flag (bool array): The padded boolean array
        """
        before, after = self.find_flag_blocks(f)
        after += 1

        inds = np.where(np.subtract(before[1:],after[:-1]) < min_gap)[0]
        after[inds] = before[inds+1]

        before -= before_pad
        after += after_pad

        padded = np.zeros_like(f)

        for b, a in zip(before, after):
            if (a-after_pad)-(b+before_pad) > min_length:
                padded[np.max([0,b]):a] = True

        return padded

    def plot_find_peak(self, freq, resp, peak_ind, save_plot=True,
            save_name=None):
        """
        Plots the output of find_freq.

        Args:
        -----
        freq (float array): The frequency data
        resp (float array): The response to full_band_resp
        peak_ind (int array): The indicies of peaks found

        Opt Args:
        ---------
        save_plot (bool): Whether to save the plot
        save_name (str): THe name of the plot
        """
        if save_plot:
            plt.ioff()
        else:
            plt.ion()

        # Break out components
        Idat = np.real(resp)
        Qdat = np.imag(resp)
        phase = np.unwrap(np.arctan2(Qdat, Idat))

        # Plot
        fig, ax = plt.subplots(2, sharex=True, figsize=(6,4))
        ax[0].plot(freq, np.abs(resp), label='amp', color='b')
        ax[0].plot(freq, Idat, label='I', color='r', linestyle=':', alpha=.5)
        ax[0].plot(freq, Qdat, label='Q', color='g', linestyle=':', alpha=.5)
        ax[0].legend(loc='lower right')
        ax[1].plot(freq, phase, color='b')
        ax[1].set_ylim((-np.pi, np.pi))

        if len(peak_ind):  # empty array returns False
            ax[0].plot(freq[peak_ind], np.abs(resp[peak_ind]), 'x', color='k')
            ax[1].plot(freq[peak_ind], phase[peak_ind], 'x', color='k')
        else:
            self.log('No peak_ind values.', self.LOG_USER)

        fig.suptitle("Peak Finding")
        ax[1].set_xlabel("Frequency offset from Subband Center (MHz)")
        ax[0].set_ylabel("Response")
        ax[1].set_ylabel("Phase [rad]")

        if save_plot:
            if save_name is None:
                self.log('Using default name for saving: find_peak.png \n' +
                    'Highly recommended that you input a non-default name')
                save_name = 'find_peak.png'
            else:
                self.log('Plotting saved to {}'.format(save_name))

            path = os.path.join(self.plot_dir, save_name)
            plt.savefig(path, bbox_inches='tight')
            self.pub.register_file(path, 'find_freq', plot=True)

            plt.close()


    def eta_fit(self, freq, resp, peak_freq, delta_freq,
                make_plot=False, plot_chans=[], save_plot=True, band=None,
                timestamp=None, res_num=None, use_slow_eta=False):
        """
        Cyndia's eta finding code.

        Args:
        -----
        freq (float array): The frequency data
        resp (float array): The response data
        peak_freq (float): The frequency of the resonance peak
        delta_freq (float): The width of frequency to calculate values

        Opt Args:
        ---------
        make_plot (bool): Whether to make plots. Default is False.
        save_plot (bool): Whether to save plots. Default is True.
        plot_chans (int array): The channels to plot. If an empty array, it
            will make plots for all channels.
        band (int): Only used for plotting - the band number of the resontaor
        timestamp (str): The timestamp of the data.
        res_num (int): The resonator number

        Rets:
        -----
        eta (complex): The eta parameter
        eta_scaled (complex): The eta parameter divided by subband_half_Width
        eta_phase_deg (float): The angle to rotate IQ circle
        r2 (float): The R^2 value copared to the resonator fit
        eta_mag (float): The amplitude of eta
        latency (float): THe delay
        Q (float): THe resonator quality factor
        """

        if band is None:
            # assume all bands have the same number of channels, and
            # pull the number of channels from the first band in the
            # list of bands specified in experiment.cfg.
            bands = self.config.get('init').get('bands')
            band = bands[0]

        n_subbands = self.get_number_sub_bands(band)
        digitizer_frequency_mhz = self.get_digitizer_frequency_mhz(band)
        subband_half_width = digitizer_frequency_mhz/\
            n_subbands

        if timestamp is None:
            timestamp = self.get_timestamp()

        amp = np.abs(resp)

        try:
            left = np.where(freq < peak_freq - delta_freq)[0][-1]
        except IndexError:
            left = 0
        try:
            left_plot = np.where(freq < peak_freq - 5*delta_freq)[0][-1]
        except IndexError:
            left = 0

        right = np.where(freq > peak_freq + delta_freq)[0][0]
        right_plot = np.where(freq > peak_freq + 5*delta_freq)[0][0]

        eta = (freq[right] - freq[left]) / (resp[right] - resp[left])

        if use_slow_eta:
            band_center = self.get_band_center_mhz(band)
            f_slow, resp_slow, eta_slow = self.eta_estimator(band,
                peak_freq*1.0E-6+band_center)

        # Get eta parameters
        latency = (np.unwrap(np.angle(resp))[-1] -
            np.unwrap(np.angle(resp))[0]) / (freq[-1] - freq[0])/2/np.pi
        eta_mag = np.abs(eta)
        eta_angle = np.angle(eta)
        eta_scaled = eta_mag / subband_half_width
        eta_phase_deg = eta_angle * 180 / np.pi


        if left != right:
            sk_fit = tools.fit_skewed_lorentzian(freq[left_plot:right_plot],
                amp[left_plot:right_plot])
            r2 = np.sum((amp[left_plot:right_plot] -
                tools.skewed_lorentzian(freq[left_plot:right_plot],
                *sk_fit))**2)
            Q = sk_fit[5]
        else:
            r2 = np.nan
            Q = np.nan

        if make_plot:
            if len(plot_chans) == 0:
                self.log('Making plot for band' +
                    ' {} res {:03}'.format(band, res_num))
                self.plot_eta_fit(freq[left_plot:right_plot],
                    resp[left_plot:right_plot],
                    eta=eta, eta_mag=eta_mag, r2=r2,
                    save_plot=save_plot, timestamp=timestamp, band=band,
                    res_num=res_num, sk_fit=sk_fit, f_slow=f_slow, resp_slow=resp_slow)
            else:
                if res_num in plot_chans:
                    self.log('Making plot for band ' +
                        '{} res {:03}'.format(band, res_num))
                    self.plot_eta_fit(freq[left_plot:right_plot],
                        resp[left_plot:right_plot],
                        eta=eta, eta_mag=eta_mag, eta_phase_deg=eta_phase_deg,
                        r2=r2, save_plot=save_plot, timestamp=timestamp,
                        band=band, res_num=res_num, sk_fit=sk_fit,
                        f_slow=f_slow, resp_slow=resp_slow)

        return eta, eta_scaled, eta_phase_deg, r2, eta_mag, latency, Q


    def plot_eta_fit(self, freq, resp, eta=None, eta_mag=None, peak_freq=None,
            eta_phase_deg=None, r2=None, save_plot=True, plotname_append='',
            show_plot=False, timestamp=None, res_num=None, band=None,
            sk_fit=None, f_slow=None, resp_slow=None, channel=None):
        """
        Plots the eta parameter fits. This is called by self.eta_fit or
        plot_tune_summary.

        Args:
        -----
        freq (float array): The frequency data
        resp (complex array): THe response data

        Opt Args:
        ---------
        eta (complex): The eta parameter
        eta_mag (complex): The amplitude of the eta parameter
        eta_phase_deg (float): The angle of the eta parameter in degrees
        r2 (float): The R^2 value
        save_plot (bool): Whether to save the plot. Default True.
        plotname_append (string): Appended to the default plot filename. Default ''.
        timestamp (str): The timestamp to name the file
        res_num (int): The resonator number to label the plot
        band (int): The band number to label the plot
        sk_fit (flot array): The fit parameters for the skewe lorentzian
        """
        if timestamp is None:
            timestamp = self.get_timestamp()

        if show_plot:
            plt.ion()
        else:
            plt.ioff()

        I = np.real(resp)
        Q = np.imag(resp)
        amp = np.sqrt(I**2 + Q**2)
        phase = np.unwrap(np.arctan2(Q, I))  # radians

        if peak_freq is not None:
            plot_freq = freq - peak_freq
        else:
            plot_freq = freq

        plot_freq = plot_freq * 1.0E3

        fig = plt.figure(figsize=(9,4.5))
        gs=GridSpec(2,3)
        ax0 = fig.add_subplot(gs[0,0])
        ax1 = fig.add_subplot(gs[1,0], sharex=ax0)
        ax2 = fig.add_subplot(gs[:,1:])
        ax0.plot(plot_freq, I, label='I', linestyle=':', color='k')
        ax0.plot(plot_freq, Q, label='Q', linestyle='--', color='k')
        ax0.scatter(plot_freq, amp, c=np.arange(len(freq)), s=3,
            label='amp')
        zero_idx = np.ravel(np.where(plot_freq == 0))[0]
        ax0.plot(plot_freq[zero_idx], amp[zero_idx], 'x', color='r')
        if sk_fit is not None:
            ax0.plot(plot_freq, tools.skewed_lorentzian(plot_freq*1.0E6,
                *sk_fit), color='r', linestyle=':')
        ax0.legend(fontsize=10, loc='lower right')
        ax0.set_ylabel('Resp')

        ax1.scatter(plot_freq, np.rad2deg(phase), c=np.arange(len(freq)), s=3)
        ax1.set_ylabel('Phase [deg]')
        ax1.set_xlabel('Freq [kHz]')

        # write what refPhaseDelay and refPhaseDelayFine were on the
        # phase plot, since we typically look at it when trying to
        # optimize them.
        bbox = dict(boxstyle="round", ec='w', fc='w', alpha=.65)
        ax1.text(.03, .15,
            'refPhaseDelay={}'.format(self.get_ref_phase_delay(band)),
            transform=ax1.transAxes, fontsize=8, bbox=bbox)
        ax1.text(.03, .05,
            'refPhaseDelayFine={}'.format(self.get_ref_phase_delay_fine(band)),
            transform=ax1.transAxes, fontsize=8, bbox=bbox)

        # IQ circle
        ax2.axhline(0, color='k', linestyle=':', alpha=.5)
        ax2.axvline(0, color='k', linestyle=':', alpha=.5)

        ax2.scatter(I, Q, c=np.arange(len(freq)), s=3)
        ax2.set_xlabel('I')
        ax2.set_ylabel('Q')

        if peak_freq is not None:
            ax0.text(.03, .9, '{:5.2f} MHz'.format(peak_freq),
                transform=ax0.transAxes, fontsize=10,
                bbox=bbox)

        lab = ''
        if eta is not None:
            if eta_mag is not None:
                lab = r'$\eta/\eta_{mag}$' + \
                    ': {:4.3f}+{:4.3f}'.format(np.real(eta/eta_mag),
                    np.imag(eta/eta_mag)) + '\n'
            else:
                lab = lab + r'$\eta$' + ': {}'.format(eta) + '\n'
        if eta_mag is not None:
            lab = lab + r'$\eta_{mag}$' + ': {:1.3e}'.format(eta_mag) + '\n'
        if eta_phase_deg is not None:
            lab = lab + r'$\eta_{ang}$' + \
                ': {:3.2f}'.format(eta_phase_deg) + '\n'
        if r2 is not None:
            lab = lab + r'$R^2$' + ' :{:4.3f}'.format(r2)
        ax2.text(.03, .81, lab, transform=ax2.transAxes, fontsize=10,
            bbox=bbox)

        if channel is not None:
            ax2.text(.85, .92, 'Ch {:03}'.format(channel),
                transform=ax2.transAxes, fontsize=10,
                bbox=bbox)

        if eta is not None:
            if eta_mag is not None:
                eta = eta/eta_mag
            respp = eta*resp
            Ip = np.real(respp)
            Qp = np.imag(respp)
            ax2.scatter(Ip, Qp, c=np.arange(len(freq)), cmap='inferno', s=3)

        if f_slow is not None and resp_slow is not None:
            self.log('Adding slow eta scan')
            mag_scale = 5E5
            band_center = self.get_band_center_mhz(band)

            resp_slow /= mag_scale
            I_slow = np.real(resp_slow)
            Q_slow = np.imag(resp_slow)
            phase_slow = np.unwrap(np.arctan2(Q_slow, I_slow))  # radians

            ax0.scatter(f_slow-band_center, np.abs(resp_slow),
                c=np.arange(len(f_slow)), cmap='Greys', s=3)
            ax1.scatter(f_slow-band_center, np.rad2deg(phase_slow),
                c=np.arange(len(f_slow)), cmap='Greys', s=3)
            ax2.scatter(I_slow, Q_slow, c=np.arange(len(f_slow)), cmap='Greys',
                s=3)

        plt.tight_layout()

        if save_plot:
            if res_num is not None and band is not None:
                save_name = '{}_eta_b{}_res{:03}{}.png'.format(timestamp, band,
                    res_num, plotname_append)
            else:
                save_name = '{}_eta{}.png'.format(timestamp, plotname_append)

            path = os.path.join(self.plot_dir, save_name)
            plt.savefig(path, bbox_inches='tight')
            self.pub.register_file(path, 'eta', plot=True)

        if not show_plot:
            plt.close()


    def get_closest_subband(self, f, band, as_offset=True):
        """
        Gives the closest subband number for a given input frequency.
        Args:
        -----
        f (float): The frequency to search for a subband
        band (int): The band to identify
        Ret:
        ----
        subband (int): The subband that contains the frequency
        """
        # get subband centers:
        subbands, centers = self.get_subband_centers(band, as_offset=as_offset)
        if self.check_freq_scale(f, centers[0]):
            pass
        else:
            raise ValueError('{} and {}'.format(f, centers[0]))

        idx = np.argmin([abs(x - f) for x in centers])
        return idx


    def check_freq_scale(self, f1, f2):
        """
        Makes sure that items are the same frequency scale (ie MHz, kHZ, etc.)
        Args:
        -----
        f1 (float): The first frequency
        f2 (float): The second frequency
        Ret:
        ----
        same_scale (bool): Whether the frequency scales are the same
        """
        if abs(f1/f2) > 1e3:
            return False
        else:
            return True


    def load_master_assignment(self, band, filename):
        """
        By default, pysmurf loads the most recent master assignment.
        Use this function to overwrite the default one.

        Args:
        -----
        band (int): The band for the master assignment file
        filename (str): The full path to the new master assignment
            file. Should be in self.tune_dir.
        """
        if 'band_{}'.format(band) in self.channel_assignment_files.keys():
            self.log('Old master assignment file:'+
                     ' {}'.format(self.channel_assignment_files['band_{}'.format(band)]))
        self.channel_assignment_files['band_{}'.format(band)] = filename
        self.log('New master assignment file:'+
                 ' {}'.format(self.channel_assignment_files['band_{}'.format(band)]))


    def get_master_assignment(self, band):
        """
        Returns the master assignment list.
        Args:
        -----
        band (int) : The band number
        Ret:
        ----
        freqs (float array): The frequency of the resonators
        subbands (int array): The subbands the channels are assigned to
        channels (int array): The channels the resonators are assigned to
        groups (int array): The bias group the channel is in
        """
        fn = self.channel_assignment_files[f'band_{band}']
        self.log(f'Drawing channel assignments from {fn}')
        d = np.loadtxt(fn, delimiter=',')
        freqs = d[:,0]
        subbands = d[:,1].astype(int)
        channels = d[:,2].astype(int)
        groups = d[:,3].astype(int)

        return freqs, subbands, channels, groups


    def assign_channels(self, freq, band=None, bandcenter=None,
            channel_per_subband=4, as_offset=True, min_offset=0.1,
            new_master_assignment=False):
        """
        Figures out the subbands and channels to assign to resonators

        Args:
        -----
        freq (flot array): The frequency of the resonators. This is not the
            same as the frequency output from full_band_resp. This is only
            where the resonators are.

        Opt Args:
        ---------
        band (int): The band to assign channels
        band_center (float array): The frequency center of the band. Must supply
            band or subband center.
        channel_per_subband (int): The number of channels to assign per
            subband. Default is 4.
        min_offset (float): The minimum offset between two resonators in MHz.
            If closer, then both are ignored.

        Ret:
        ----
        subbands (int array): An array of subbands to assign resonators
        channels (int array): An array of channel numbers to assign resonators
        offsets (float array): The frequency offset from the subband center
        """
        freq = np.sort(freq)  # Just making sure its in sequential order

        if band is None and bandcenter is None:
            self.log('Must have band or bandcenter', self.LOG_ERROR)
            raise ValueError('Must have band or bandcenter')

        subbands = np.zeros(len(freq), dtype=int)
        channels = -1 * np.ones(len(freq), dtype=int)
        offsets = np.zeros(len(freq))

        if not new_master_assignment:
            freq_master,subbands_master,channels_master,groups_master = \
                self.get_master_assignment(band)
            n_freqs = len(freq)
            n_unmatched = 0
            for idx in range(n_freqs):
                f = freq[idx]
                found_match = False
                for i in range(len(freq_master)):
                    f_master = freq_master[i]
                    if np.absolute(f-f_master) < min_offset:
                        ch = channels_master[i]
                        channels[idx] = ch
                        sb =  subbands_master[i]
                        subbands[idx] = sb
                        g = groups_master[i]
                        sb_center = self.get_subband_centers(band,
                            as_offset=as_offset)[1][sb]
                        offsets[idx] = f-sb_center
                        self.log(f'Matching {f:.2f} MHz to {f_master:.2f} MHz' +
                            ' in master channel list: assigning to ' +
                            f'subband {sb}, ch. {ch}, group {g}')
                        found_match = True
                        break
                if not found_match:
                    n_unmatched += 1
                    self.log('No match found for {:.2f} MHz'.format(f))
            self.log(f'No channel assignment for {n_unmatched} of {n_freqs}'+
                ' resonances.')
        else:
            d_freq = np.diff(freq)
            close_idx = d_freq > min_offset
            close_idx = np.logical_and(np.hstack((close_idx, True)),
                                       np.hstack((True, close_idx)))
            # Assign all frequencies to a subband
            for idx in range(len(freq)):
                subbands[idx] = self.get_closest_subband(freq[idx], band,
                                                     as_offset=as_offset)
                subband_center = self.get_subband_centers(band,
                                          as_offset=as_offset)[1][subbands[idx]]
                offsets[idx] = freq[idx] - subband_center

            # Assign unique channel numbers
            for unique_subband in set(subbands):
                chans = self.get_channels_in_subband(band, int(unique_subband))
                mask = np.where(subbands == unique_subband)[0]
                if len(mask) > channel_per_subband:
                    concat_mask = mask[:channel_per_subband]
                else:
                    concat_mask = mask[:]

                chans = chans[:len(list(concat_mask))] #I am so sorry

                channels[mask[:len(chans)]] = chans

            # Prune channels that are too close
            channels[~close_idx] = -1

            # write the channel assignments to file
            self.write_master_assignment(band, freq, subbands, channels)

        return subbands, channels, offsets


    def write_master_assignment(self, band, freqs, subbands, channels,
            bias_groups=None):
        '''
        Writes a comma-separated list in the form band, freq (MHz), subband,
        channel, group. Group number defaults to -1. The order of inputs is
        legacy and weird.

        Args:
        -----
        band (int array) : A list of bands
        freqs (float array) : A list of frequencies
        subbands (int array) : A list of subbands
        channels (int array) : A list of channel numbers

        Opt Args:
        ---------
        bias_groups (int array) : A list of bias groups. If None,
           populates the array with -1.
        '''
        timestamp = self.get_timestamp()
        if bias_groups is None:
            bias_groups = -np.ones(len(freqs),dtype=int)

        fn = os.path.join(self.tune_dir,
                          f'{timestamp}_channel_assignment_b{band}.txt')
        self.log('Writing new channel assignment to {}'.format(fn))
        f = open(fn,'w')
        for i in range(len(channels)):
            f.write('%.4f,%i,%i,%i\n' % (freqs[i],subbands[i],channels[i],
                bias_groups[i]))
        f.close()

        self.load_master_assignment(band, fn)


    def make_master_assignment_from_file(self, band, tuning_filename):
        """
        Makes a master assignment file

        Args:
        -----
        band (int) : The band number
        tuning_filename : The tuning file to use for generating the
            master_assignemnt
        """
        self.log('Drawing band-{} tuning data from {}'.format(band,
            tuning_filename))

        # Load the tuning file
        d = np.load(tuning_filename).item()[band]['resonances']

        # Extrac the values from the tuning file
        freqs = []
        subbands = []
        channels = []
        for i in range(len(d)):
            freqs.append(d[i]['freq'])
            subbands.append(d[i]['subband'])
            channels.append(d[i]['channel'])

        self.write_master_assignment(band, freqs, subbands, channels)


    def get_group_list(self, band, group):
        """
        Returns a list of all the channels in a band and bias
        group. Note that it is possible to have channels that are
        on the same bias group but different bands.

        Args:
        ----
        band (int) : The band number.
        group (int) : The bias group number.

        Ret:
        ----
        bias_group_list (int array) : The list of channels that
            are in the band and bias group.
        """
        _, _, channels, groups = self.get_master_assignment(band)
        chs_in_group = []
        for i in range(len(channels)):
            if groups[i] == group:
                chs_in_group.append(channels[i])
        return np.array(chs_in_group)


    def get_group_number(self, band, ch):
        """
        Gets the bias group number of a band, channel pair. The
        master_channel_assignment must be filled.

        Args:
        -----
        band (int) : The band number
        ch (int) : The channel number

        Ret:
        ----
        bias_group (int) : The bias group number
        """
        _, _, channels,groups = self.get_master_assignment(band)
        for i in range(len(channels)):
            if channels[i] == ch:
                return groups[i]
        return None


    def write_group_assignment(self, bias_group_dict):
        '''
        Combs master channel assignment and assigns group number to all channels
        in ch_list. Does not affect other channels in the master file.

        Args:
        -----
        bias_group_dict (dict) : The output of identify_bias_groups
        '''
        bias_groups = list(bias_group_dict.keys())

        # Find all unique bands
        bands = np.array([], dtype=int)
        for bg in bias_groups:
            for b in bias_group_dict[bg]['band']:
                if b not in bands:
                    bands = np.append(bands, b)

        for b in bands:
            # Load previous channel assignment
            freqs_master, subbands_master, channels_master, \
                groups_master = self.get_master_assignment(b)
            for bg in bias_groups:
                for i in np.arange(len(bias_group_dict[bg]['channel'])):
                    ch = bias_group_dict[bg]['channel'][i]
                    bb = bias_group_dict[bg]['band'][i]

                    # First check they are in the same band
                    if bb == b:
                        idx = np.ravel(np.where(channels_master == ch))
                        if len(idx) == 1:
                            groups_master[idx] = bg

            # Save the new master channel assignment
            self.write_master_assignment(b, freqs_master,
                                         subbands_master,
                                         channels_master,
                                         bias_groups=groups_master)


    def relock(self, band, res_num=None, drive=None, r2_max=.08,
            q_max=100000, q_min=0, check_vals=False, min_gap=None,
            write_log=False):
        """
        Turns on the tones. Also cuts bad resonators.

        Args:
        -----
        band (int): The band to relock
        Opt args:
        ---------
        res_num (int array): The resonators to lock. If None, tries all the
            resonators.
        drive (int): The tone amplitudes to set
        check_vals (bool) : Whether to check r2 and Q values. Default is False.
        r2_max (float): The highest allowable R^2 value
        q_max (float): The maximum resonator Q factor
        q_min (float): The minimum resonator Q factor
        min_gap (float) : Thee minimum distance between resonators.
        """
        n_channels = self.get_number_channels(band)

        self.log('Relocking...')
        if res_num is None:
            res_num = np.arange(n_channels)
        else:
            res_num = np.array(res_num)

        if drive is None:
            drive = self.freq_resp[band]['drive']

        amplitude_scale = np.zeros(n_channels)
        center_freq = np.zeros(n_channels)
        feedback_enable = np.zeros(n_channels)
        eta_phase = np.zeros(n_channels)
        eta_mag = np.zeros(n_channels)

        # Extract frequencies from dictionary
        f = [self.freq_resp[band]['resonances'][k]['freq']
            for k in self.freq_resp[band]['resonances'].keys()]

        # Populate arrays
        counter = 0
        for k in self.freq_resp[band]['resonances'].keys():
            ch = self.freq_resp[band]['resonances'][k]['channel']
            idx = np.where(f == self.freq_resp[band]['resonances'][k]['freq'])[0][0]
            f_gap = None
            if len(f) > 1:
                f_gap = np.min(np.abs(np.append(f[:idx], f[idx+1:])-f[idx]))
            if write_log:
                self.log(f'Res {k:03} - Channel {ch}')
            for ll, hh in self.bad_mask:
                # Check again bad mask list
                if f[idx] > ll and f[idx] < hh:
                    self.log(f'{f[idx]:4.3f} in bad list.')
                    ch = -1
            if ch < 0:
                if write_log:
                    self.log(f'No channel assigned: res {k:03}')
            elif min_gap is not None and f_gap is not None and f_gap < min_gap:
                # Resonators too close
                if write_log:
                    self.log(f'Closest resonator is {f_gap:3.3f} MHz away')
            elif self.freq_resp[band]['resonances'][k]['r2'] > r2_max and check_vals:
                # chi squared cut
                if write_log:
                    self.log(f'R2 too high: res {k:03}')
            elif k not in res_num:
                if write_log:
                    self.log('Not in resonator list')
            else:
                # Channels passed all checks so actually turn on
                center_freq[ch] = self.freq_resp[band]['resonances'][k]['offset']
                amplitude_scale[ch] = drive
                feedback_enable[ch] = 1
                eta_phase[ch] = self.freq_resp[band]['resonances'][k]['eta_phase']
                eta_mag[ch] = self.freq_resp[band]['resonances'][k]['eta_scaled']
                counter += 1

        # Set the actual variables
        self.set_center_frequency_array(band, center_freq, write_log=write_log,
            log_level=self.LOG_INFO)
        self.set_amplitude_scale_array(band, amplitude_scale.astype(int),
            write_log=write_log, log_level=self.LOG_INFO)
        self.set_feedback_enable_array(band, feedback_enable.astype(int),
            write_log=write_log, log_level=self.LOG_INFO)
        self.set_eta_phase_array(band, eta_phase, write_log=write_log,
            log_level=self.LOG_INFO)
        self.set_eta_mag_array(band, eta_mag, write_log=write_log,
            log_level=self.LOG_INFO)

        self.log('Setting on {} channels on band {}'.format(counter, band),
            self.LOG_USER)

    def fast_relock(self, band):
        """
        """
        self.log('Fast relocking with: {}'.format(self.tune_file))
        self.set_tune_file_path(self.tune_file)
        self.set_load_tune_file(band, 1)
        self.log('Done fast relocking')

    def _get_eta_scan_result_from_key(self, band, key):
        """
        Convenience function to get values from the freq_resp dictionary.

        Args:
        -----
        band (int) : The 500 MHz band to get values from
        key (str) : The dictionary value to read out

        Ret:
        ----
        val (array) : The array of values associated with key.
        """
        if 'resonances' not in self.freq_resp[band].keys():
            self.log('No tuning. Run setup_notches() or load_tune()')
            return None

        return np.array([self.freq_resp[band]['resonances'][k][key]
                         for k in self.freq_resp[band]['resonances'].keys()])


    def get_eta_scan_result_freq(self, band):
        """
        Convenience function that gets the frequency results from eta scans.

        Args:
        -----
        band (int) : The band

        Ret:
        ----
        freq (float array) : The frequency in MHz of the resonators.
        """
        return self._get_eta_scan_result_from_key(band, 'freq')


    def get_eta_scan_result_eta(self, band):
        """
        Convenience function that gets thee eta values from eta scans.

        Args:
        -----
        band (int) : The band

        Ret:
        ----
        eta (complex array) : The eta of the resonators.
        """
        return self._get_eta_scan_result_from_key(band, 'eta')


    def get_eta_scan_result_eta_mag(self, band):
        """
        Convenience function that gets thee eta mags from
        eta scans.

        Args:
        -----
        band (int) : The band

        Ret:
        ----
        eta_mag (float array) : The eta of the resonators.
        """
        return self._get_eta_scan_result_from_key(band, 'eta_mag')


    def get_eta_scan_result_eta_scaled(self, band):
        """
        Convenience function that gets the eta scaled from
        eta scans. eta_scaled is eta_mag/digitizer_freq_mhz/n_subbands

        Args:
        -----
        band (int) : The band

        Ret:
        ----
        eta_mag (float array) : The eta_scaled of the resonators.
        """
        return self._get_eta_scan_result_from_key(band, 'eta_scaled')


    def get_eta_scan_result_eta_phase(self, band):
        """
        Convenience function that gets the eta phase values from
        eta scans.

        Args:
        -----
        band (int) : The band

        Ret:
        ----
        eta_phase (float array) : The eta_phase of the resonators.
        """
        return self._get_eta_scan_result_from_key(band, 'eta_phase')


    def get_eta_scan_result_channel(self, band):
        """
        Convenience function that gets the channel assignments from
        eta scans.

        Args:
        -----
        band (int) : The band

        Ret:
        ----
        channels (int array) : The channels of the resonators.
        """
        return self._get_eta_scan_result_from_key(band, 'channel')


    def get_eta_scan_result_subband(self, band):
        """
        Convenience function that gets the subband from eta scans.

        Args:
        -----
        band (int) : The band

        Ret:
        ----
        subband (float array) : The subband of the resonators.
        """
        return self._get_eta_scan_result_from_key(band, 'subband')


    def get_eta_scan_result_offset(self, band):
        """
        Convenience function that gets the offset from center frequency
        from eta scans.

        Args:
        -----
        band (int) : The band

        Ret:
        ----
        offset (float array) : The offset from the subband centers  of
           the resonators.
        """
        return self._get_eta_scan_result_from_key(band, 'offset')


    def eta_estimator(self, band, freq, drive=10, f_sweep_half=.3,
                      df_sweep=.002, delta_freq=.01,
                      lock_max_derivative=False):
        """
        Estimates eta parameters using the slow eta_scan.

        Args:
        -----
        band (int) : The 500 MHz band
        freq (float) : The frequency to scan

        Opt Args:
        ---------
        drive (int) : The tone ampltidue. Default is 10.
        f_sweep_half (float) : The frequency to sweep.
        df_sweep (float) : The freqeucny step size
        """
        subband, offset = self.freq_to_subband(band, freq)
        f_sweep = np.arange(offset-f_sweep_half, offset+f_sweep_half, df_sweep)
        f, resp = self.fast_eta_scan(band, subband, f_sweep, 2, drive)
        # resp = rr + 1.j*ii

        a_resp = np.abs(resp)
        if lock_max_derivative:
            self.log('Locking on max derivative instead of res min')
            deriv = np.abs(np.diff(a_resp))
            idx = np.ravel(np.where(deriv == np.max(deriv)))[0]
        else:
            idx = np.ravel(np.where(a_resp == np.min(a_resp)))[0]
        f0 = f_sweep[idx]

        try:
            left = np.where(f_sweep < f0 - delta_freq)[0][-1]
        except IndexError:
            left = 0

        try:
            right = np.where(f_sweep > f0 + delta_freq)[0][0]
        except BaseException:
            right = len(f_sweep)-1

        eta = (f_sweep[right]-f_sweep[left])/(resp[right]-resp[left])

        sb, sbc = self.get_subband_centers(band, as_offset=False)

        return f_sweep + sbc[subband], resp, eta


    def eta_scan(self, band, subband, freq, drive, write_log=False,
                 sync_group=True):
        """
        Same as slow eta scans
        """
        if len(self.which_on(band)):
            self.band_off(band, write_log=write_log)

        n_subband = self.get_number_sub_bands(band)
        n_channel = self.get_number_channels(band)
        channel_order = self.get_channel_order(band)
        first_channel = channel_order[::n_channel//n_subband]

        self.set_eta_scan_channel(band, first_channel[subband],
                                  write_log=write_log)
        self.set_eta_scan_amplitude(band, drive, write_log=write_log)
        self.set_eta_scan_freq(band, freq, write_log=write_log)
        self.set_eta_scan_dwell(band, 0, write_log=write_log)

        self.set_run_eta_scan(band, 1, wait_done=False, write_log=write_log)
        pvs = [self._cryo_root(band) + self._eta_scan_results_real,
               self._cryo_root(band) + self._eta_scan_results_imag]

        if sync_group:
            sg = SyncGroup(pvs, skip_first=False)

            sg.wait()
            vals = sg.get_values()
            rr = vals[pvs[0]]
            ii = vals[pvs[1]]
        else:
            rr = self.get_eta_scan_results_real(2, len(freq))
            ii = self.get_eta_scan_results_imag(2, len(freq))

        self.set_amplitude_scale_channel(band, first_channel[subband], 0)

        return rr, ii

    def flux_ramp_check(self, band, reset_rate_khz=None,
            fraction_full_scale=None, flux_ramp=True, save_plot=True,
            show_plot=False, setup_flux_ramp=True):
        """
        Tries to measure the V-phi curve in feedback disable mode.
        You can also run this with flux ramp off to see the intrinsic
        noise on the readout channel.
        Args:
        -----
        band (int) : The band to check.
        Opt Args:
        ---------
        reset_rate_khz (float) : The flux ramp rate in kHz.
        fraction_full_scale (float) : The amplitude of the flux ramp from
           zero to one.
        flux_ramp (bool) : Whether to flux ramp. Default is True.
        save_plot (bool) : Whether to save the plot. Default True.
        show_plot (bool) : Whether to show the plot. Default False.
        setup_flux_ramp (bool) : Whether to setup the flux ramp at the end.
            Default True.
        """
        if show_plot:
            plt.ion()
        else:
            plt.ioff()

        if reset_rate_khz is None:
            reset_rate_khz = self.reset_rate_khz
            self.log('reset_rate_khz is None. ',
                     f'Using default: {reset_rate_khz}')
        n_channels = self.get_number_channels(band)
        old_fb = self.get_feedback_enable_array(band)

        # Turn off feedback
        self.set_feedback_enable_array(band, np.zeros_like(old_fb))
        d, df, sync = self.tracking_setup(band,0, reset_rate_khz=reset_rate_khz,
            fraction_full_scale=fraction_full_scale, make_plot=False,
            save_plot=False, show_plot=False, lms_enable1=False,
            lms_enable2=False, lms_enable3=False, flux_ramp=flux_ramp,
            setup_flux_ramp=setup_flux_ramp)

        n_samp, n_chan = np.shape(df)

        dd = np.ravel(np.where(np.diff(sync[:,0]) !=0))
        first_idx = dd[0]//n_channels
        second_idx = dd[4]//n_channels
        dt = int(second_idx-first_idx)  # In slow samples
        n_fr = int(len(sync[:,0])/n_channels/dt)
        reset_idx = np.arange(first_idx, n_fr*dt + first_idx+1, dt)

        # Reset to the previous FB state
        self.set_feedback_enable_array(band, old_fb)

        fs = self.get_digitizer_frequency_mhz(band) * 1.0E6 /2/n_channels

        # Only plot channels that are on - group by subband
        chan = self.which_on(band)
        freq = np.zeros(len(chan), dtype=float)
        subband = np.zeros(len(chan), dtype=int)
        for i, c in enumerate(chan):
            freq[i] = self.channel_to_freq(band, c)
            (subband[i], _) = self.freq_to_subband(band, freq[i])

        unique_subband = np.unique(subband)

        cm = plt.get_cmap('viridis')

        timestamp = self.get_timestamp()

        self.log('Making plots...')
        scale = 1.0E3

        for sb in unique_subband:
            idx = np.ravel(np.where(subband == sb))
            chs = chan[idx]
            # fig, ax = plt.subplots(1, 2, figsize=(8,4), sharey=True)
            fig = plt.figure(figsize=(8,6))
            gs = GridSpec(2,2)
            ax0 = fig.add_subplot(gs[0,:])
            ax1 = fig.add_subplot(gs[1,0])
            ax2 = fig.add_subplot(gs[1,1])

            for i, c in enumerate(chs):
                color = cm(i/len(chs))
                ax0.plot(np.arange(n_samp)/fs*scale,
                         df[:,c], label='ch {}'.format(c),
                         color=color)
                holder = np.zeros((n_fr-1, dt))
                for i in np.arange(n_fr-1):
                    holder[i] = df[first_idx+dt*i:first_idx+dt*(i+1),c]
                ds = np.mean(holder, axis=0)
                ax1.plot(np.arange(len(ds))/fs*scale, ds, color=color)
                ff, pp = signal.welch(df[:,c], fs=fs)
                ax2.semilogy(ff/1.0E3, pp, color=color)

            for k in reset_idx:
                ax0.axvline(k/fs*scale, color='k', alpha=.6, linestyle=':')

            ax0.legend(loc='upper left')
            ax1.set_xlabel('Time [ms]')
            ax2.set_xlabel('Freq [kHz]')
            fig.suptitle('Band {} Subband {}'.format(band, sb))

            if save_plot:
                save_name = timestamp
                if not flux_ramp:
                    save_name = save_name + '_no_FR'
                save_name = save_name+ \
                    '_b{}_sb{:03}_flux_ramp_check.png'.format(band, sb)
                path = os.path.join(self.plot_dir, save_name)
                plt.savefig(path, bbox_inches='tight')
                self.pub.register_file(path, 'flux_ramp', plot=True)

                if not show_plot:
                    plt.close()

        return d, df, sync


    def tracking_setup(self, band, channel=None, reset_rate_khz=None,
            write_log=False, make_plot=False, save_plot=True, show_plot=True,
            nsamp=2**19, lms_freq_hz=None, meas_lms_freq=False,
            meas_flux_ramp_amp=False, n_phi0=4, flux_ramp=True,
            fraction_full_scale=None, lms_enable1=True, lms_enable2=True,
            lms_enable3=True, lms_gain=None, return_data=True,
            new_epics_root=None, feedback_start_frac=None,
            feedback_end_frac=None, setup_flux_ramp=True, plotname_append=''):
        """
        The function to start tracking. Starts the flux ramp and if requested
        attempts to measure the lms (demodulation) frequency. Otherwise this
        just tracks at the input lms frequency. This will also make plots for
        the channels listed in {channel} input.

        Args:
        -----
        band (int) : The band number
        Opt Args:
        ---------
        channel (int or int array) : The channels to plot
        reset_rate_khz (float) : The flux ramp frequency
        write_log (bool) : Whether to write output to the log.  Default False.
        make_plot (bool) : Whether to make plots. Default False.
        save_plot (bool) : Whether to save plots. Default True.
        plotname_append (string): Appended to the default plot filename.
            Default ''.
        show_plot (bool) : Whether to display the plot. Default True.
        lms_freq_hz (float) : The frequency of the tracking algorithm.
           Default is 4000
        flux_ramp (bool) : Whether to turn on flux ramp. Default True.
        fraction_full_scale (float) : The flux ramp amplitude, as a
           fraction of the maximum. Default is .4950.
        lms_enable1 (bool) : Whether to use the first harmonic for tracking.
           Default True.
        lms_enable2 (bool) : Whether to use the second harmonic for tracking.
           Default True.
        lms_enable3 (bool) : Whether to use the third harmonic for tracking.
           Default True.
        lms_gain (int) : The tracking gain parameters. Default is the value
           in the config table.
        feedback_start_frac (float) : The fraction of the full flux
           ramp at which to stop applying feedback in each flux ramp
           cycle.  Must be in [0,1).  Defaults to whatever's in the cfg
           file.
        feedback_end_frac (float) : The fraction of the full flux ramp
           at which to stop applying feedback in each flux ramp cycle.
           Must be >0.  Defaults to whatever's in the cfg file.
        meas_lms_freq (bool) : Whether or not to try to estimate the
           carrier rate using the flux_mod2 function.  Default false.
           lms_freq_hz must be None.
        setup_flux_ramp (bool) : Whether to setup the flux ramp. Default
           is True.
        plotname_append (str) : Optional string to append plots with.
        """
        if reset_rate_khz is None:
            reset_rate_khz = self.reset_rate_khz
        if lms_gain is None:
            lms_gain = self.lms_gain[band]

        ##
        ## Load unprovided optional args from cfg
        if feedback_start_frac is None:
            feedback_start_frac = self.config.get('tune_band').get('feedback_start_frac')[str(band)]
        if feedback_end_frac is None:
            feedback_end_frac = self.config.get('tune_band').get('feedback_end_frac')[str(band)]
        ## End loading unprovided optional args from cfg
        ##

        ##
        ## Argument validation

        # Validate feedback_start_frac and feedback_end_frac
        if (feedback_start_frac < 0) or (feedback_start_frac >= 1):
            raise ValueError("feedback_start_frac = {} not in [0,1)".format(feedback_start_frac))
        if (feedback_end_frac < 0):
            raise ValueError("feedback_end_frac = {} not > 0".format(feedback_end_frac))
        # If feedback_start_frac exceeds feedback_end_frac, then
        # there's no range of the flux ramp cycle over which we're
        # applying feedback.
        if (feedback_end_frac < feedback_start_frac):
            raise ValueError("feedback_end_frac = {} is not less than feedback_start_frac = {}".format(feedback_end_frac, feedback_start_frac))
        # Done validating feedbackStart and feedbackEnd

        ## End argument validation
        ##

        if not flux_ramp:
            self.log('WARNING: THIS WILL NOT TURN ON FLUX RAMP!')

        if make_plot:
            if show_plot:
                plt.ion()
            else:
                plt.ioff()

        if fraction_full_scale is None:
            fraction_full_scale = self.fraction_full_scale
        else:
            self.fraction_full_scale = fraction_full_scale

        # Measure either LMS freq or the flux ramp amplitude
        if lms_freq_hz is None:
            if meas_lms_freq and meas_flux_ramp_amp:
                self.log('Requested measurement of both LMS freq '+
                         'and flux ramp amplitude. Cannot do both.',
                         self.LOG_ERROR)
                return None, None, None
            elif meas_lms_freq:
                lms_freq_hz = self.estimate_lms_freq(band,
                    reset_rate_khz,fraction_full_scale=fraction_full_scale,
                    channel=channel)
            elif meas_flux_ramp_amp:
                fraction_full_scale = self.estimate_flux_ramp_amp(band,
                    n_phi0,reset_rate_khz=reset_rate_khz, channel=channel)
                lms_freq_hz = reset_rate_khz * n_phi0 * 1.0E3
            else:
                lms_freq_hz = self.config.get('tune_band').get('lms_freq')[str(band)]
            self.lms_freq_hz[band] = lms_freq_hz
            if write_log:
                self.log('Using lms_freq_estimator : {:.0f} Hz'.format(lms_freq_hz))

        if not flux_ramp:
            lms_enable1 = 0
            lms_enable2 = 0
            lms_enable3 = 0

        if write_log:
            self.log("Using lmsFreqHz = {:.0f} Hz".format(lms_freq_hz), self.LOG_USER)

        self.set_lms_gain(band, lms_gain, write_log=write_log)
        self.set_lms_enable1(band, lms_enable1, write_log=write_log)
        self.set_lms_enable2(band, lms_enable2, write_log=write_log)
        self.set_lms_enable3(band, lms_enable3, write_log=write_log)
        self.set_lms_freq_hz(band, lms_freq_hz, write_log=write_log)

        iq_stream_enable = 0  # must be zero to access f,df stream
        self.set_iq_stream_enable(band, iq_stream_enable, write_log=write_log)

        if setup_flux_ramp:
            self.flux_ramp_setup(reset_rate_khz, fraction_full_scale,
                             write_log=write_log, new_epics_root=new_epics_root)
        else:
            self.log("Not changing flux ramp status. Use setup_flux_ramp " +
                     "boolean to run flux_ramp_setup")

        # Doing this after flux_ramp_setup so that if needed we can
        # set feedback_end based on the flux ramp settings.

        # Compute feedback_start/feedback_end from
        # feedback_start_frac/feedback_end_frac.
        channel_frequency_mhz = self.get_channel_frequency_mhz(band)
        digitizer_frequency_mhz = self.get_digitizer_frequency_mhz(band)
        feedback_start = int(
            feedback_start_frac*(self.get_ramp_max_cnt()+1)/(
                digitizer_frequency_mhz/channel_frequency_mhz/2. ) )
        feedback_end = int(
            feedback_end_frac*(self.get_ramp_max_cnt()+1)/(
                digitizer_frequency_mhz/channel_frequency_mhz/2. ) )

        # Set feedbackStart and feedbackEnd
        self.set_feedback_start(band, feedback_start, write_log=write_log)
        self.set_feedback_end(band, feedback_end, write_log=write_log)

        if write_log:
            self.log("Applying feedback over "+
                f"{(feedback_end_frac-feedback_start_frac)*100.:.1f}% of each "+
                f"flux ramp cycle (with feedbackStart={feedback_start} and " +
                f"feedbackEnd={feedback_end})", self.LOG_USER)

        if flux_ramp:
            self.flux_ramp_on(write_log=write_log, new_epics_root=new_epics_root)

        # take one dataset with all channels
        if return_data or make_plot:
            f, df, sync = self.take_debug_data(band, IQstream=iq_stream_enable,
                single_channel_readout=0, nsamp=nsamp)

            df_std = np.std(df, 0)
            df_channels = np.ravel(np.where(df_std >0))

            # Intersection of channels that are on and have some flux ramp resp
            channels_on = list(set(df_channels) & set(self.which_on(band)))

            self.log(f"Number of channels on : {len(self.which_on(band))}",
                     self.LOG_USER)
            self.log("Number of channels on with flux ramp "+
                f"response : {len(channels_on)}", self.LOG_USER)

            f_span = np.max(f,0) - np.min(f,0)

        if make_plot:
            timestamp = self.get_timestamp()

            fig,ax = plt.subplots(1,3,figsize = (12,5))
            fig.suptitle(f'LMS freq = {lms_freq_hz:.0f} Hz, '+
                f'n_channels = {len(channels_on)}')

            # Histogram the stddev
            ax[0].hist(df_std[channels_on] * 1e3,bins = 20,edgecolor = 'k')
            ax[0].set_xlabel('Flux ramp demod error std (kHz)')
            ax[0].set_ylabel('number of channels')

            # Histogram the max-min flux ramp amplitude response
            ax[1].hist(f_span[channels_on] * 1e3,bins = 20,edgecolor='k')
            ax[1].set_xlabel('Flux ramp amplitude (kHz)')
            ax[1].set_ylabel('number of channels')

            # Plot df vs resp amplitude
            ax[2].plot(f_span[channels_on]*1e3, df_std[channels_on]*1e3, '.')
            ax[2].set_xlabel('FR Amp (kHz)')
            ax[2].set_ylabel('RF demod error (kHz)')
            x = np.array([0, np.max(f_span[channels_on])*1.0E3])

            # useful line to guide the eye
            y_factor = 100
            y = x/y_factor
            ax[2].plot(x, y, color='k', linestyle=':',label=f'1:{y_factor}')
            ax[2].legend(loc='best')

            fig.tight_layout(rect=[0, 0.03, 1, 0.95])

            if save_plot:
                path = os.path.join(self.plot_dir,
                    timestamp + '_FR_amp_v_err' + plotname_append + '.png')
                plt.savefig(path, bbox_inches='tight')
                self.pub.register_file(path, 'amp_vs_err', plot=True)

            if not show_plot:
                # If we don't want a live view, close the plot
                plt.close()

            if channel is not None:
                channel = np.ravel(np.array(channel))
                sync_idx = self.make_sync_flag(sync)

                bbox = dict(boxstyle="round", ec='w', fc='w', alpha=.65)

                for ch in channel:
                    # Setup plotting
                    fig, ax = plt.subplots(2, sharex=True, figsize=(9, 4.75))

                    # Plot tracked component
                    ax[0].plot(f[:, ch]*1e3)
                    ax[0].set_ylabel('Tracked Freq [kHz]')
                    ax[0].text(.025, .9,
                        'LMS Freq {:.0f} Hz'.format(lms_freq_hz), fontsize=10,
                        transform=ax[0].transAxes, bbox=bbox, ha='left',
                        va='top')

                    ax[0].text(.95, .9, 'Band {} Ch {:03}'.format(band, ch),
                        fontsize=10, transform=ax[0].transAxes, ha='right',
                        va='top', bbox=bbox)

                    # Plot the untracking part
                    ax[1].plot(df[:, ch]*1e3)
                    ax[1].set_ylabel('Freq Error [kHz]')
                    ax[1].set_xlabel('Samp Num')
                    ax[1].text(.025, .9,
                        f'RMS error = {df_std[ch]*1e3:.2f} kHz\n' +
                        f'FR frac. full scale = {fraction_full_scale:.2f}',
                        fontsize=10, transform=ax[1].transAxes, bbox=bbox,
                        ha='left', va='top')

                    n_sync_idx = len(sync_idx)
                    for i, s in enumerate(sync_idx):
                        # Lines for reset
                        ax[0].axvline(s, color='k', linestyle=':', alpha=.5)
                        ax[1].axvline(s, color='k', linestyle=':', alpha=.5)

                        # highlight used regions
                        if i < n_sync_idx-1:
                            n_samp = sync_idx[i+1]-sync_idx[i]
                            start = s + feedback_start_frac*n_samp
                            end = s + feedback_end_frac*n_samp

                            ax[0].axvspan(start, end, color='k', alpha=.15)
                            ax[1].axvspan(start, end, color='k', alpha=.15)
                    plt.tight_layout()

                    if save_plot:
                        path = os.path.join(self.plot_dir, timestamp +
                            '_FRtracking_band{}_ch{:03}{}.png'.format(band,ch,
                            plotname_append))
                        plt.savefig(path, bbox_inches='tight')
                        self.pub.register_file(path, 'tracking', plot=True)

                    if not show_plot:
                        plt.close()

        self.set_iq_stream_enable(band, 1, write_log=write_log)

        if return_data:
            return f, df, sync


    def track_and_check(self, band, channel=None, reset_rate_khz=None,
            make_plot=False, save_plot=True, show_plot=True,
            lms_freq_hz=None, flux_ramp=True, fraction_full_scale=None,
            lms_enable1=True, lms_enable2=True, lms_enable3=True, lms_gain=None,
            f_min=.015, f_max=.2, df_max=.03, toggle_feedback=True,
            relock=True, tracking_setup=True,
            feedback_start_frac=None, feedback_end_frac=None, setup_flux_ramp=True):
        """
        This runs tracking setup and check_lock to prune bad channels. This has
        all the same inputs and tracking_setup and check_lock. In particular the
        cut parameters are f_min, f_max, and df_max.

        Args:
        -----
        band (int): The band to track and check

        Opt Args:
        ---------
        channel (int or int array): List of channels to plot.
        toggle_feedback (bool): Whether or not to reset feedback (both the
            global band feedbackEnable and the lmsEnables between tracking_setup
            and  check_lock.
        relock (bool): Whether or not to relock at the start. Default True.
        tracking_setup (bool): Whether or not to run tracking_setup. Default
            True.
        reset_rate_khz (float) : The flux ramp frequency
        write_log (bool) : Whether to write output to the log.  Default False.
        make_plot (bool) : Whether to make plots. Default False.
        save_plot (bool) : Whether to save plots. Default True.
        show_plot (bool) : Whether to display the plot. Default True.
        lms_freq_hz (float) : The frequency of the tracking algorithm.
           Default is 4000
        flux_ramp (bool) : Whether to turn on flux ramp. Default True.
        fraction_full_scale (float) : The flux ramp amplitude, as a
           fraction of the maximum. Default is .4950.
        lms_enable1 (bool) : Whether to use the first harmonic for tracking.
           Default True.
        lms_enable2 (bool) : Whether to use the second harmonic for tracking.
           Default True.
        lms_enable3 (bool) : Whether to use the third harmonic for tracking.
           Default True.
        lms_gain (int) : The tracking gain parameters. Default is the value
           in the config file
        feedback_start_frac (float) : The fraction of the full flux
           ramp at which to stop applying feedback in each flux ramp
           cycle.  Must be in [0,1).  Defaults to whatever's in the cfg
           file.
        feedback_end_frac (float) : The fraction of the full flux ramp
           at which to stop applying feedback in each flux ramp cycle.
           Must be >0.  Defaults to whatever's in the cfg file.
        meas_lms_freq (bool) : Whether or not to try to estimate the
           carrier rate using the flux_mod2 function.  Default false.
           lms_freq_hz must be None.
        f_min (float) : The maximum frequency swing.
        f_max (float) : The minimium frequency swing
        df_max (float) : The maximum value of the stddev of df
        setup_flux_ramp (bool) : Whether to setup the flux ramp at the end.
            Default True.
        """
        if reset_rate_khz is None:
            reset_rate_khz = self.reset_rate_khz
        if lms_gain is None:
            lms_gain = self.lms_gain[band]

        if relock:
            self.relock(band)

        # Start tracking
        if tracking_setup:
            self.tracking_setup(band, channel=channel,
                reset_rate_khz=reset_rate_khz, make_plot=make_plot,
                save_plot=save_plot, show_plot=show_plot,
                lms_freq_hz=lms_freq_hz, flux_ramp=flux_ramp,
                fraction_full_scale=fraction_full_scale, lms_enable1=lms_enable1,
                lms_enable2=lms_enable2, lms_enable3=lms_enable3,
                lms_gain=lms_gain, return_data=False,
                feedback_start_frac=feedback_start_frac,
                feedback_end_frac=feedback_end_frac,
                setup_flux_ramp=setup_flux_ramp)

        # Toggle the feedback because sometimes tracking exits in a bad state.
        # I'm not sure if this is still the case, but no reason to stop doing
        # this. -EY 20191001
        if toggle_feedback:
            self.toggle_feedback(band)

        # Check the lock status and cut channels based on inputs.
        self.check_lock(band, f_min=f_min, f_max=f_max, df_max=df_max,
            make_plot=make_plot, flux_ramp=flux_ramp,
            fraction_full_scale=fraction_full_scale, lms_freq_hz=lms_freq_hz,
            reset_rate_khz=reset_rate_khz,
            feedback_start_frac=feedback_start_frac,
            feedback_end_frac=feedback_end_frac,
            setup_flux_ramp=setup_flux_ramp)


    def eta_phase_check(self, band, rot_step_size=30, rot_max=360,
            reset_rate_khz=None, fraction_full_scale=None, flux_ramp=True):
        """
        """
        if reset_rate_khz is None:
            reset_rate_khz = self.reset_rate_khz

        ret = {}

        eta_phase0 = self.get_eta_phase_array(band)
        ret['eta_phase0'] = eta_phase0
        ret['band'] = band
        n_channels = self.get_number_channels(band)

        old_fb = self.get_feedback_enable_array(band)
        self.set_feedback_enable_array(band, np.zeros_like(old_fb))

        rot_ang = np.arange(0, rot_max, rot_step_size)
        ret['rot_ang'] = rot_ang
        ret['data'] = {}

        for i, r in enumerate(rot_ang):
            self.log('Rotating {:3.1f} deg'.format(r))
            eta_phase = np.zeros_like(eta_phase0)
            for c in np.arange(n_channels):
                eta_phase[c] = tools.limit_phase_deg(eta_phase0[c] + r)
            self.set_eta_phase_array(band, eta_phase)

            d, df, sync = self.tracking_setup(band,0,
                reset_rate_khz=reset_rate_khz,
                fraction_full_scale=fraction_full_scale,
                make_plot=False, save_plot=False, show_plot=False,
                lms_enable1=False, lms_enable2=False, lms_enable3=False,
                flux_ramp=flux_ramp)

            ret['data'][r] = {}
            ret['data'][r]['df'] = df
            ret['data'][r]['sync'] = sync

        self.set_feedback_enable_array(band, old_fb)
        self.set_eta_phase_array(2, eta_phase0)

        return ret


    def analyze_eta_phase_check(self, dat, channel):
        """
        """
        keys = dat['data'].keys()
        band = dat['band']
        n_keys = len(keys)

        n_channels = self.get_number_channels(band)
        fs = self.get_digitizer_frequency_mhz(band) * 1.0E6 /2/n_channels
        scale = 1.0E3

        fig, ax = plt.subplots(1)
        cm = plt.get_cmap('viridis')
        for j, k in enumerate(keys):
            sync = dat['data'][k]['sync']
            df = dat['data'][k]['df']
            dd = np.ravel(np.where(np.diff(sync[:,0]) !=0))
            first_idx = dd[0]//n_channels
            second_idx = dd[4]//n_channels
            dt = int(second_idx-first_idx)  # In slow samples
            n_fr = int(len(sync[:,0])/n_channels/dt)

            holder = np.zeros((n_fr-1, dt))
            for i in np.arange(n_fr-1):
                holder[i] = df[first_idx+dt*i:first_idx+dt*(i+1), channel]
            ds = np.mean(holder, axis=0)

            color = cm(j/n_keys)
            ax.plot(np.arange(len(ds))/fs*scale, ds, color=color,
                    label='{:3.1f}'.format(k))

        ax.legend()
        ax.set_title('Band {} Ch {:03}'.format(band, channel))
        ax.set_xlabel('Time [ms]')


    _num_flux_ramp_dac_bits = 16
    _cryo_card_flux_ramp_relay_bit = 16
    _cryo_card_relay_wait = 0.25 #sec

    def unset_fixed_flux_ramp_bias(self,acCouple=True):
        """
        Alias for setting ModeControl=0
        """

        # make sure flux ramp is configured off before switching back into mode=1
        self.flux_ramp_off()

        self.log("Setting flux ramp ModeControl to 0.",self.LOG_USER)
        self.set_mode_control(0)

        ## Don't want to flip relays more than we have to.  Check if it's in the correct
        ## position ; only explicitly flip to DC if we have to.
        if acCouple and (self.get_cryo_card_relays() >>
                self._cryo_card_flux_ramp_relay_bit & 1):
            self.log("Flux ramp set to DC mode (rly=0).",
                     self.LOG_USER)
            self.set_cryo_card_relay_bit(self._cryo_card_flux_ramp_relay_bit,0)

            # make sure it gets picked up by cryo card before handing back
            while (self.get_cryo_card_relays() >>
                    self._cryo_card_flux_ramp_relay_bit & 1):
                self.log("Waiting for cryo card to update",
                         self.LOG_USER)
                time.sleep(self._cryo_card_relay_wait)


    def set_fixed_flux_ramp_bias(self,fractionFullScale,debug=True,
            do_config=True):
        """
        ???
        Args:
        -----
        fractionFullScale (float) : Fraction of full flux ramp scale to output
        from [-1,1]
        """

        # fractionFullScale must be between [0,1]
        if abs(np.abs(fractionFullScale))>1:
            raise ValueError(f"fractionFullScale = {fractionFullScale} not "+
                "in [-1,1].")

        ## Disable flux ramp if it was on
        ## Doesn't seem to effect the fixed DC value being output
        ## if already in fixed flux ramp mode ModeControl=1
        self.flux_ramp_off()

        ## Don't want to flip relays more than we have to.  Check if it's in the correct
        ## position ; only explicitly flip to DC if we have to.
        if not (self.get_cryo_card_relays() >> self._cryo_card_flux_ramp_relay_bit & 1):
            self.log("Flux ramp relay is either in AC mode or we haven't set " +
                "it yet - explicitly setting to DC mode (=1).", self.LOG_USER)
            self.set_cryo_card_relay_bit(self._cryo_card_flux_ramp_relay_bit,1)

            while not (self.get_cryo_card_relays() >>
                    self._cryo_card_flux_ramp_relay_bit & 1):
                self.log("Waiting for cryo card to update", self.LOG_USER)
                time.sleep(self._cryo_card_relay_wait)

        if do_config:
            ## ModeControl must be 1
            mode_control=self.get_mode_control()
            if not mode_control==1:

                #before switching to ModeControl=1, make sure DAC is set to output zero V
                LTC1668RawDacData0=np.floor(0.5*(2**self._num_flux_ramp_dac_bits))
                self.log("Before switching to fixed DC flux ramp output, " +
                         " explicitly setting flux ramp DAC to zero "+
                         "(LTC1668RawDacData0={})".format(mode_control,LTC1668RawDacData0),
                         self.LOG_USER)
                self.set_flux_ramp_dac(LTC1668RawDacData0)

                self.log("Flux ramp ModeControl is {}".format(mode_control) +
                         " - changing to 1 for fixed DC output.",
                         self.LOG_USER)
                self.set_mode_control(1)

        ## Compute and set flux ramp DAC to requested value
        LTC1668RawDacData = np.floor((2**self._num_flux_ramp_dac_bits) *
            (1-np.abs(fractionFullScale))/2)
        ## 2s complement
        if fractionFullScale<0:
            LTC1668RawDacData = 2**self._num_flux_ramp_dac_bits-LTC1668RawDacData-1
        if debug:
            self.log("Setting flux ramp to {}".format(100 * fractionFullScale,
                     int(LTC1668RawDacData)) + "% of full scale (LTC1668RawDacData={})",
                     self.LOG_USER)
        self.set_flux_ramp_dac(LTC1668RawDacData)


    def flux_ramp_setup(self, reset_rate_khz, fraction_full_scale, df_range=.1,
            band=2, write_log=False, new_epics_root=None):
        """
        Set flux ramp sawtooth rate and amplitude. If there are errors, check
        that you are using an allowed reset rate! Not all rates are allowed.
        Allowed rates: 1, 2, 3, 4, 5, 6, 8, 10, 12, 15 kHz
        Args:
        -----
        reset_rate_khz (int) : The flux ramp rate to set in kHz. The allowable
            values are 1, 2, 3, 4, 5, 6, 8, 10, 12, 15 kHz
        fraction_full_scale (float) : The amplitude of the flux ramp as a
            fraction of the maximum possible value.
        Opt Args:
        ---------
        df_range (float) :
        band (int) : The band to setup the flux ramp on.
        write_log (bool) : Whether to write output to the log
        new_epics_root (str) : Override the original epics root.
        """

        # Disable flux ramp
        self.flux_ramp_off(new_epics_root=new_epics_root,
                           write_log=write_log)

        digitizerFrequencyMHz = self.get_digitizer_frequency_mhz(band,
            new_epics_root=new_epics_root)
        dspClockFrequencyMHz=digitizerFrequencyMHz/2

        desiredRampMaxCnt = ((dspClockFrequencyMHz*1e3)/
            (reset_rate_khz)) - 1
        rampMaxCnt = np.floor(desiredRampMaxCnt)

        resetRate = (dspClockFrequencyMHz * 1e6) / (rampMaxCnt + 1)

        HighCycle = 5 # not sure why these are hardcoded
        LowCycle = 5
        rtmClock = (dspClockFrequencyMHz * 1e6) / (HighCycle + LowCycle + 2)
        trialRTMClock = rtmClock

        fullScaleRate = fraction_full_scale * resetRate
        desFastSlowStepSize = (fullScaleRate * 2**self.num_flux_ramp_counter_bits) / rtmClock
        trialFastSlowStepSize = round(desFastSlowStepSize)
        FastSlowStepSize = trialFastSlowStepSize

        trialFullScaleRate = trialFastSlowStepSize * trialRTMClock / (2**self.num_flux_ramp_counter_bits)

        trialResetRate = (dspClockFrequencyMHz * 1e6) / (rampMaxCnt + 1)
        trialFractionFullScale = trialFullScaleRate / trialResetRate
        fractionFullScale = trialFractionFullScale
        diffDesiredFractionFullScale = np.abs(trialFractionFullScale -
            fraction_full_scale)

        self.log("Percent full scale = {:0.3f}%".format(100 * fractionFullScale),
            self.LOG_USER)

        if diffDesiredFractionFullScale > df_range:
            raise ValueError("Difference from desired fraction of full scale " +
                "exceeded! {}".format(diffDesiredFractionFullScale) +
                " vs acceptable {}".format(df_range))
            self.log("Difference from desired fraction of full scale exceeded!" +
                " P{} vs acceptable {}".format(diffDesiredFractionFullScale,
                    df_range),
                self.LOG_USER)

        if rtmClock < 2e6:
            raise ValueError("RTM clock rate = "+
                "{} is too low (SPI clock runs at 1MHz)".format(rtmClock*1e-6))
            self.log("RTM clock rate = "+
                "{} is too low (SPI clock runs at 1MHz)".format(rtmClock * 1e-6),
                self.LOG_USER)
            return


        FastSlowRstValue = np.floor((2**self.num_flux_ramp_counter_bits) *
            (1 - fractionFullScale)/2)


        KRelay = 3 #where do these values come from
        SelectRamp = self.get_select_ramp(new_epics_root=new_epics_root) # from config file
        RampStartMode = self.get_ramp_start_mode(new_epics_root=new_epics_root) # from config file
        PulseWidth = 64
        DebounceWidth = 255
        RampSlope = 0
        ModeControl = 0
        EnableRampTrigger = 1

        self.set_low_cycle(LowCycle, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_high_cycle(HighCycle, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_k_relay(KRelay, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_ramp_max_cnt(rampMaxCnt, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_select_ramp(SelectRamp, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_ramp_start_mode(RampStartMode, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_pulse_width(PulseWidth, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_debounce_width(DebounceWidth, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_ramp_slope(RampSlope, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_mode_control(ModeControl, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_fast_slow_step_size(FastSlowStepSize, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_fast_slow_rst_value(FastSlowRstValue, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_enable_ramp_trigger(EnableRampTrigger, new_epics_root=new_epics_root,
            write_log=write_log)
        self.set_ramp_rate(reset_rate_khz, new_epics_root=new_epics_root,
            write_log=write_log)



    def get_fraction_full_scale(self, new_epics_root=None):
        """
        Returns the fraction_full_scale.
        Opt Args:
        ---------
        new_epics_root (str) : Overrides the initialized epics root.
        Ret:
        ----
        fraction_full_scale (float) : The fraction of the flux ramp amplitude
        """
        return 1-2*(self.get_fast_slow_rst_value(new_epics_root=new_epics_root)/
                    2**self.num_flux_ramp_counter_bits)


    def check_lock(self, band, f_min=.015, f_max=.2, df_max=.03,
            make_plot=False, flux_ramp=True, fraction_full_scale=None,
            lms_freq_hz=None, reset_rate_khz=None, feedback_start_frac=None,
            feedback_end_frac=None, setup_flux_ramp=True, **kwargs):
        """
        Takes a tracking setup and turns off channels that have bad
        tracking. The limits are set by the variables f_min, f_max,
        and df_max. The output is stored to freq_resp[band]['lock_status'] dict.

        Args:
        -----
        band (int) : The band the check
        Opt Args:
        ---------
        f_min (float) : The maximum frequency swing.
        f_max (float) : The minimium frequency swing
        df_max (float) : The maximum value of the stddev of df
        make_plot (bool) : Whether to make plots. Default False
        flux_ramp (bool) : Whether to flux ramp or not. Default True
        faction_full_scale (float): Number between 0 and 1. The amplitude
           of the flux ramp.
        lms_freq_hz (float) : The tracking frequency in Hz. Default is None
        reset_rate_khz (float) : The flux ramp reset rate in kHz.
        feedback_start_frac (float) : What fraction of the flux ramp to
            skip before feedback. Float between 0 and 1.
        feedback_end_frac (float) : What fraction of the flux ramp to skip
            at the end of feedback. Float between 0 and 1.
        setup_flux_ramp (bool) : Whether to setup the flux ramp at the end.
            Default is True.
        """
        self.log('Checking lock on band {}'.format(band))

        if reset_rate_khz is None:
            reset_rate_khz = self.reset_rate_khz

        if fraction_full_scale is None:
            fraction_full_scale = self.fraction_full_scale

        if lms_freq_hz is None:
            lms_freq_hz = self.lms_freq_hz[band]

        channels = self.which_on(band)
        n_chan = len(channels)

        self.log('Currently {} channels on'.format(n_chan))

        # Tracking setup returns information on all channels in a band
        f, df, sync = self.tracking_setup(band, make_plot=False,
            flux_ramp=flux_ramp, fraction_full_scale=fraction_full_scale,
            lms_freq_hz=lms_freq_hz, reset_rate_khz=reset_rate_khz,
            feedback_start_frac=feedback_start_frac,
            feedback_end_frac=feedback_end_frac)

        high_cut = np.array([])
        low_cut = np.array([])
        df_cut = np.array([])

        # Make cuts
        for ch in channels:
            f_chan = f[:,ch]
            f_span = np.max(f_chan) - np.min(f_chan)
            df_rms = np.std(df[:,ch])

            if f_span > f_max:
                self.set_amplitude_scale_channel(band, ch, 0, **kwargs)
                high_cut = np.append(high_cut, ch)
            elif f_span < f_min:
                self.set_amplitude_scale_channel(band, ch, 0, **kwargs)
                low_cut = np.append(low_cut, ch)
            elif df_rms > df_max:
                self.set_amplitude_scale_channel(band, ch, 0, **kwargs)
                df_cut = np.append(df_cut, ch)

        chan_after = self.which_on(band)

        self.log('High cut channels {}'.format(high_cut))
        self.log('Low cut channels {}'.format(low_cut))
        self.log('df cut channels {}'.format(df_cut))
        self.log('Good channels {}'.format(chan_after))
        self.log('High cut count: {}'.format(len(high_cut)))
        self.log('Low cut count: {}'.format(len(low_cut)))
        self.log('df cut count: {}'.format(len(df_cut)))
        self.log('Started with {}. Now {}'.format(n_chan, len(chan_after)))

        # Store the data in freq_resp
        timestamp = self.get_timestamp(as_int=True)
        self.freq_resp[band]['lock_status'][timestamp] = {
            'action' : 'check_lock',
            'flux_ramp': flux_ramp,
            'f_min' : f_min,
            'f_max' : f_max,
            'df_max' : df_max,
            'high_cut' : high_cut,
            'low_cut' : low_cut,
            'channels_before' : channels,
            'channels_after' : chan_after
        }


    def check_lock_flux_ramp_off(self, band,df_max=.03,
            make_plot=False, **kwargs):
        """
        Simple wrapper function for check_lock with the flux ramp off
        """
        self.check_lock(band, f_min=0., f_max=np.inf, df_max=df_max,
            make_plot=make_plot, flux_ramp=False, **kwargs)


    def find_freq(self, band, subband=np.arange(13,115), drive_power=None,
            n_read=2, make_plot=False, save_plot=True, plotname_append='',
            window=50, rolling_med=True, make_subband_plot=False,
            show_plot=False, grad_cut=.05, amp_cut=.25, pad=2, min_gap=2):
        '''
        Finds the resonances in a band (and specified subbands)
        Args:
        -----
        band (int) : The band to search
        Optional Args:
        --------------
        subband (int) : An int array for the subbands
        drive_power (int) : The drive amplitude.  If none given, takes from cfg.
        n_read (int) : The number sweeps to do per subband
        make_plot (bool) : make the plot frequency sweep. Default False.
        save_plot (bool) : save the plot. Default True.
        plotname_append (string): Appended to the default plot filename. Default ''.
        rolling_med (bool) : Whether to iterate on a rolling median or just
           the median of the whole sample.
        window (int) : The width of the rolling median window
        pad (int): number of samples to pad on either side of a resonance search
            window
        min_gap (int): minimum number of samples between resonances
        grad_cut (float): The value of the gradient of phase to look for
            resonances. Default is .05
        amp_cut (float): The fractional distance from the median value to decide
            whether there is a resonance. Default is .25.
        '''

        # Turn off all tones in this band first.  May want to make
        # this only turn off tones in each sub-band before sweeping,
        # instead?
        self.band_off(band)

        if drive_power is None:
            drive_power = self.config.get('init')[f'band_{band}'].get('amplitude_scale')
            self.log(f'No drive_power given. Using value in config ' +
                f'file: {drive_power}')

        self.log('Sweeping across frequencies')
        f, resp = self.full_band_ampl_sweep(band, subband, drive_power, n_read)

        timestamp = self.get_timestamp()

        # Save data
        save_name = '{}_amp_sweep_{}.txt'

        path = os.path.join(self.output_dir, save_name.format(timestamp, 'freq'))
        np.savetxt(path, f)
        self.pub.register_file(path, 'sweep_response', format='txt')

        path = os.path.join(self.output_dir, save_name.format(timestamp, 'resp'))
        np.savetxt(path, resp)
        self.pub.register_file(path, 'sweep_response', format='txt')

        # Place in dictionary - dictionary declared in smurf_control
        self.freq_resp[band]['find_freq'] = {}
        self.freq_resp[band]['find_freq']['subband'] = subband
        self.freq_resp[band]['find_freq']['f'] = f
        self.freq_resp[band]['find_freq']['resp'] = resp
        if 'timestamp' in self.freq_resp[band]['find_freq']:
            self.freq_resp[band]['timestamp'] = \
                np.append(self.freq_resp[band]['find_freq']['timestamp'], timestamp)
        else:
            self.freq_resp[band]['find_freq']['timestamp'] = np.array([timestamp])

        # Find resonator peaks
        res_freq = self.find_all_peak(self.freq_resp[band]['find_freq']['f'],
            self.freq_resp[band]['find_freq']['resp'], subband,
            make_plot=make_plot, plotname_append=plotname_append, band=band,
            rolling_med=rolling_med, window=window,
            make_subband_plot=make_subband_plot, grad_cut=grad_cut,
            amp_cut=amp_cut, pad=pad, min_gap=min_gap)
        self.freq_resp[band]['find_freq']['resonance'] = res_freq

        # Save resonances
        path = os.path.join(self.output_dir,
            save_name.format(timestamp, 'resonance'))
        np.savetxt(path, self.freq_resp[band]['find_freq']['resonance'])
        self.pub.register_file(path, 'resonances', format='txt')

        # Call plotting
        if make_plot:
            self.plot_find_freq(self.freq_resp[band]['find_freq']['f'],
                self.freq_resp[band]['find_freq']['resp'], save_plot=save_plot,
                show_plot=show_plot,
                save_name=save_name.replace('.txt', plotname_append +
                                            '.png').format(timestamp, band))


        return f, resp

    def plot_find_freq(self, f=None, resp=None, subband=None, filename=None,
            save_plot=True, save_name='amp_sweep.png', show_plot=False):
        '''
        Plots the response of the frequency sweep. Must input f and resp, or
        give a path to a text file containing the data for offline plotting.
        To do:
        Add ability to use timestamp and multiple plots

        Opt Args:
        ---------
        f (float array) : An array of frequency data
        resp (complex array) : An array of find_freq response values
        subband (int array): A list of subbands that are scanned
        filename (str) : The full path to the file where the find_freq
            is stored.
        save_plot (bool) : save the plot. Default True.
        show_plot (bool) : Whether to show the plot. Defualt False.
        save_name (string) : What to name the plot. default find_freq.png
        '''
        if subband is None:
            subband = np.arange(128)
        subband = np.asarray(subband)

        if (f is None or resp is None) and filename is None:
            self.log('No input data or file given. Nothing to plot.')
            return
        else:
            if filename is not None:
                f, resp = np.load(filename)

            cm = plt.cm.get_cmap('viridis')
            plt.figure(figsize=(10,4))

            for i, sb in enumerate(subband):
                color = cm(float(i)/len(subband)/2. + .5*(i%2))
                plt.plot(f[sb,:], np.abs(resp[sb,:]), '.', markersize=4,
                    color=color)
            plt.title("find_freq response")
            plt.xlabel("Frequency offset (MHz)")
            plt.ylabel("Normalized Amplitude")

            if save_plot:
                path = os.path.join(self.plot_dir, save_name)
                plt.savefig(path, bbox_inches='tight')
                self.pub.register_file(path, 'response', plot=True)

            if show_plot:
                plt.show()
            else:
                plt.close()


    def full_band_ampl_sweep(self, band, subband, drive, n_read, n_step=121):
        """sweep a full band in amplitude, for finding frequencies
        args:
        -----
            band (int) = bandNo (500MHz band)
            subband (int) = which subbands to sweep
            drive (int) = drive power (defaults to 10)
            n_read (int) = numbers of times to sweep, defaults to 2
        returns:
        --------
            freq (list, n_freq x 1) = frequencies swept
            resp (array, n_freq x 2) = complex response
        """
        digitizer_freq = self.get_digitizer_frequency_mhz(band)  # in MHz
        n_subbands = self.get_number_sub_bands(band)

        scan_freq = (digitizer_freq/n_subbands/2)*np.linspace(-1,1,n_step)

        resp = np.zeros((n_subbands, np.shape(scan_freq)[0]), dtype=complex)
        freq = np.zeros((n_subbands, np.shape(scan_freq)[0]))

        subband_nos, subband_centers = self.get_subband_centers(band)

        self.log('Working on band {:d}'.format(band))
        for sb in subband:
            self.log(f'Sweeping subband no: {sb}')
            f, r = self.fast_eta_scan(band, sb, scan_freq, n_read,
                                      drive)
            resp[sb,:] = r
            freq[sb,:] = f
            freq[sb,:] = scan_freq + \
                subband_centers[subband_nos.index(sb)]

        return freq, resp


    def find_all_peak(self, freq, resp, subband=None, rolling_med=False,
            window=500, grad_cut=0.05, amp_cut=0.25, freq_min=-2.5E8,
            freq_max=2.5E8, make_plot=False, save_plot=True, plotname_append='',
            band=None, make_subband_plot=False, subband_plot_with_slow=False,
            timestamp=None, pad=2, min_gap=2):
        """
        find the peaks within each subband requested from a fullbandamplsweep
        Args:
        -----
        freq (array):  (n_subbands x n_freq_swept) array of frequencies swept
        response (complex array): n_subbands x n_freq_swept array of complex
            response
        subbands (list of ints): subbands that we care to search in

        Optional Args:
        --------------
        rolling_med (bool): whether to use a rolling median for the background
        window (int): number of samples to window together for rolling med
        grad_cut (float): The value of the gradient of phase to look for
            resonances. Default is .05
        amp_cut (float): The fractional distance from the median value to decide
            whether there is a resonance. Default is .25.
        freq_min (float): The minimum frequency relative to the center of
            the band to look for resonances. Units of Hz. Defaults is -2.5E8
        freq_max (float): The maximum frequency relative to the center of
            the band to look for resonances. Units of Hz. Defaults is 2.5E8
        make_plot (bool): Whether to make a plot. Default is False.
        make_subband_plot (bool): Whether to make a plot per subband. This is
            very slow. Default is False.
        save_plot (bool): Whether to save the plot to self.plot_dir. Default
            is True.
        plotname_append (string): Appended to the default plot filename.
            Default is ''.
        band (int): The band to take find the peaks in. Mainly for saving
            and plotting.
        timestamp (str): The timestamp. Mainly for saving and plotting
        pad (int): number of samples to pad on either side of a resonance search
            window
        min_gap (int): minimum number of samples between resonances
        grad_kernel_width (int) : The number of samples to take after a point
            to calculate the gradient of phase. Default is 8.

        Ret:
        ----
        peaks (float array) : The frequency of all the peaks found
        """
        peaks = np.array([])
        timestamp = self.get_timestamp()

        # Stack all the frequency and response data into a
        sb, _ = np.where(freq !=0)
        idx = np.unique(sb)
        f_stack = np.ravel(freq[idx])
        r_stack = np.ravel(resp[idx])

        # Frequency is interleaved, so sort it
        s = np.argsort(f_stack)
        f_stack = f_stack[s]
        r_stack = r_stack[s]

        # Now find the peaks
        peaks = self.find_peak(f_stack, r_stack, rolling_med=rolling_med,
            window=window, grad_cut=grad_cut, amp_cut=amp_cut, freq_min=freq_min,
            freq_max=freq_max, make_plot=make_plot, save_plot=save_plot,
            plotname_append=plotname_append, band=band,
            make_subband_plot=make_subband_plot,
            subband_plot_with_slow=subband_plot_with_slow, timestamp=timestamp,
            pad=pad, min_gap=min_gap)

        return peaks


    def fast_eta_scan(self, band, subband, freq, n_read, drive,
            make_plot=False):
        """copy of fastEtaScan.m from Matlab. Sweeps quickly across a range of
        freq and gets I, Q response
        Args:
         band (int): which 500MHz band to scan
         subband (int): which subband to scan
         freq (n_freq x 1 array): frequencies to scan relative to subband
            center
         n_read (int): number of times to scan
         drive (int): tone power
        Optional Args:
        make_plot (bool): Make eta plots
        Outputs:
         resp (n_freq x 2 array): real, imag response as a function of
            frequency
         freq (n_freq x n_read array): frequencies scanned, relative to
            subband center
        """
        n_subbands = self.get_number_sub_bands(band)
        n_channels = self.get_number_channels(band)

        channel_order = self.get_channel_order(band)

        channels_per_subband = int(n_channels / n_subbands)
        first_channel_per_subband = channel_order[0::channels_per_subband]
        subchan = first_channel_per_subband[subband]

        self.set_eta_scan_freq(band, freq)
        self.set_eta_scan_amplitude(band, drive)
        self.set_eta_scan_channel(band, subchan)
        self.set_eta_scan_dwell(band, 0)

        self.set_run_eta_scan(band, 1)

        I = self.get_eta_scan_results_real(band, count=len(freq))
        Q = self.get_eta_scan_results_imag(band, count=len(freq))

        self.band_off(band)

        response = np.zeros((len(freq), ), dtype=complex)

        for index in range(len(freq)):
            Ielem = I[index]
            Qelem = Q[index]
            if Ielem > 2**23:
                Ielem = Ielem - 2**24
            if Qelem > 2**23:
                Qelem = Qelem - 2**24

            Ielem /= 2**23
            Qelem /= 2**23

            response[index] = Ielem + 1j*Qelem

        if make_plot:
            # To do : make plotting
            self.log('Plotting does not work in this function yet...')

        return freq, response

    def setup_notches(self, band, resonance=None, drive=None,
                      sweep_width=.3, df_sweep=.002, min_offset=0.1,
                      delta_freq=None, new_master_assignment=False,
                      lock_max_derivative=False):
        """
        Does a fine sweep over the resonances found in find_freq. This
        information is used for placing tones onto resonators. It is
        recommended that you follow this up with run_serial_gradient_descent()
        afterwards.

        Args:
        -----
        band (int) : The 500 MHz band to setup.
        Optional Args:
        --------------
        resonance (float array) : A 2 dimensional array with resonance
            frequencies and the subband they are in. If given, this will take
            precedent over the one in self.freq_resp.
        drive (int) : The power to drive the resonators. Default is defined in cfg file.
        sweep_width (float) : The range to scan around the input resonance in
            units of MHz. Default .3
        df_sweep (float) : The sweep step size in MHz. Default .005
        min_offset (float): Minimum distance in MHz between two resonators for assigning channels.
        delta_freq (float): The frequency offset at which to measure
            the complex transmission to compute the eta parameters.
            Passed to eta_estimator.  Units are MHz.  If none supplied
            as an argument, takes value in config file.
        new_master_assignment (bool): Whether to create a new master assignment
            file. This file defines the mapping between resonator frequency
            and channel number.
        """
        # Turn off all tones in this band first
        self.band_off(band)

        # Check if any resonances are stored
        if 'find_freq' not in self.freq_resp[band]:
            self.log(f'No find_freq in freq_resp dictionary for band {band}. ' +
                     'Run find_freq first.', self.LOG_ERROR)
            return
        elif 'resonance' not in self.freq_resp[band]['find_freq'] and resonance is None:
            self.log('No resonances stored in band {}'.format(band) +
                '. Run find_freq first.', self.LOG_ERROR)
            return

        if drive is None:
            drive = self.config.get('init')['band_{}'.format(band)].get('amplitude_scale')
            self.log('No drive given. Using value in config file: {}'.format(drive))

        if delta_freq is None:
            delta_freq = self.config.get('tune_band').get('delta_freq')[str(band)]

        if resonance is not None:
            input_res = resonance
        else:
            input_res = self.freq_resp[band]['find_freq']['resonance']

        n_subbands = self.get_number_sub_bands(band)
        digitizer_frequency_mhz = self.get_digitizer_frequency_mhz(band)
        subband_half_width = digitizer_frequency_mhz/\
            n_subbands

        self.freq_resp[band]['drive'] = drive

        # Loop over inputs and do eta scans
        resonances = {}
        band_center = self.get_band_center_mhz(band)
        input_res = input_res + band_center

        n_res = len(input_res)
        for i, f in enumerate(input_res):
            self.log('freq {:5.4f} - {} of {}'.format(f, i+1, n_res))
            freq, resp, eta = self.eta_estimator(band, f, drive,
                                                 f_sweep_half=sweep_width,
                                                 df_sweep=df_sweep,
                                                 delta_freq=delta_freq,
                                                 lock_max_derivative=lock_max_derivative)
            eta_phase_deg = np.angle(eta)*180/np.pi
            eta_mag = np.abs(eta)
            eta_scaled = eta_mag / subband_half_width

            abs_resp = np.abs(resp)
            idx = np.ravel(np.where(abs_resp == np.min(abs_resp)))[0]

            f_min = freq[idx]

            resonances[i] = {
                'freq' : f_min,
                'eta' : eta,
                'eta_scaled' : eta_scaled,
                'eta_phase' : eta_phase_deg,
                'r2' : 1,  # This is BS
                'eta_mag' : eta_mag,
                'latency': 0,  # This is also BS
                'Q' : 1,  # This is also also BS
                'freq_eta_scan' : freq,
                'resp_eta_scan' : resp
            }


        # Assign resonances to channels
        self.log('Assigning channels')
        f = [resonances[k]['freq'] for k in resonances.keys()]

        subbands, channels, offsets = self.assign_channels(f, band=band,
            as_offset=False, min_offset=min_offset,
            new_master_assignment=new_master_assignment)

        for i, k in enumerate(resonances.keys()):
            resonances[k].update({'subband': subbands[i]})
            resonances[k].update({'channel': channels[i]})
            resonances[k].update({'offset': offsets[i]})

        self.freq_resp[band]['resonances'] = resonances

        self.save_tune()

        self.relock(band)


    def save_tune(self, update_last_tune=True):
        """
        Saves the tuning information (self.freq_resp) to tuning directory
        """
        timestamp = self.get_timestamp()
        savedir = os.path.join(self.tune_dir, timestamp+"_tune")
        self.log('Saving to : {}.npy'.format(savedir))
        np.save(savedir, self.freq_resp)
        self.pub.register_file(savedir, 'tune', format='npy')

        self.tune_file = savedir+'.npy'

        return savedir + ".npy"


    def load_tune(self, filename=None, override=True, last_tune=True, band=None):
        """
        Loads the tuning information (self.freq_resp) from tuning directory
        Opt Args:
        ---------
        filename (str) : The name of the tuning.
        last_tune (bool): Whether to use the most recent tuning
            file. Default is True.
        override (bool) : Whether to replace self.freq_resp. Default
            is True.
        band (int, int array) : if None, loads entire tune.  If band
            number is provided, only loads the tune for that band.
            Not used at all unless override=True.
        """
        if filename is None and last_tune:
            filename = self.last_tune()
            self.log('Defaulting to last tuning: {}'.format(filename))
        elif filename is not None and last_tune:
            self.log('filename explicitly given. Overriding last_tune bool in load_tune.')

        fs = np.load(filename, allow_pickle=True).item()
        self.log('Done loading tuning')

        if override:
            if band is None:
                bands_in_file=list(fs.keys())
                self.log('Loading tune data for all bands={}.'.format(str(bands_in_file)))
                self.freq_resp = fs
                # Load all tune data for all bands in file.  only
                # update tune_file if both band are loaded from the
                # same file right now.  May want to handle this
                # differently to allow loading different tunes for
                # different bands, etc.
                self.tune_file = filename
            else:
                # Load only the tune data for the requested band(s).
                band=np.ravel(np.array(band))
                self.log('Only loading tune data for bands={}.'.format(str(band)))
                for b in band:
                    self.freq_resp[b] = fs[b]
        else:
            # Right now, returns tune data for all bands in file;
            # doesn't know about the band arg.
            return fs


    def last_tune(self):
        """
        Returns the full path to the most recent tuning file.
        """
        return np.sort(glob.glob(os.path.join(self.tune_dir,
                                              '*_tune.npy')))[-1]


    def estimate_lms_freq(self, band, reset_rate_khz,
                          fraction_full_scale=None,
                          new_epics_root=None, channel=None,
                          make_plot=False):
        """
        Attempts to estimate the carrier (phi0) rate for all channels
        on in the requested 500 MHz band (0..7) using the flux_mod2
        routine.

        Args:
        -----
        band (int): Will attempt to estimate the carrier rate on the
                    channels which are on in this band.
        reset_rate_khz (float): The flux ramp reset rate (in kHz).
        Opt Args:
        ---------
        fraction_full_scale (float): Passed on to the internal
                                     tracking_setup call - the
                                     fraction of full scale exercised
                                     by the flux ramp.  Defaults to
                                     value in cfg.
        new_epics_root (str): Passed on to internal tracking_setup
                              call ; If using a different RTM to flux
                              ramp, the epics root of the pyrogue
                              server controlling that RTM..  Defaults
                              to None.
        channel (int array): Passed on to the internal flux_mod2 call.
                             Which channels (if any) to plot.  Default
                             is None.
        make_plot (bool): Whether or not to make plots.
        Ret:
        ----
        The estimated lms frequency in Hz
        """
        if fraction_full_scale is None:
            fraction_full_scale = \
                self.config.get('tune_band').get('fraction_full_scale')

        old_feedback = self.get_feedback_enable(band)

        self.set_feedback_enable(band, 0)
        f, df, sync = self.tracking_setup(band, 0, make_plot=False,
            flux_ramp=True, fraction_full_scale=fraction_full_scale,
            reset_rate_khz=reset_rate_khz, lms_freq_hz=0,
            new_epics_root=new_epics_root)

        s = self.flux_mod2(band, df, sync,
                           make_plot=make_plot, channel=channel)

        self.set_feedback_enable(band, old_feedback)
        return reset_rate_khz * s * 1000  # convert to Hz


    def estimate_flux_ramp_amp(self, band, n_phi0, write_log=True,
                               reset_rate_khz=None,
                               new_epics_root=None, channel=None):
        """
        This is like estimate_lms_freq, except it changes the
        flux ramp amplitude instead of the flux ramp frequency.

        Args:
        -----
        band (int) : The beand to measure
        n_phi0 (float) : The number of phi0 desired per flux
            ramp cycle. It is recommended, but not required that
            this is an integer.

        Opt Args:
        ---------
        write_log (bool) : Whether to write log messages. Default True.
        reset_rate_khz (bool) : The reset (or flux ramp cycle) frequency
            in kHz. If None, reads the current value. Default is None.
        channel (int array) : The channels to use to estimate the
            amplitude. If None, uses all channels that are on. Default
            None.
        """
        start_fraction_full_scale = self.get_fraction_full_scale()
        if write_log:
            self.log('Starting fraction full scale : '+
                     f'{start_fraction_full_scale:1.3f}')

        if reset_rate_khz is None:
            reset_rate_khz = self.get_flux_ramp_freq()

        # Get old feedback status to reset it at the end
        old_feedback = self.get_feedback_enable(band)
        self.set_feedback_enable(band, 0)

        f, df, sync = self.tracking_setup(band, 0, make_plot=False,
            flux_ramp=True, fraction_full_scale=start_fraction_full_scale,
            reset_rate_khz=reset_rate_khz, lms_freq_hz=0,
            new_epics_root=new_epics_root)

        # Set feedback to original value
        self.set_feedback_enable(band, old_feedback)

        # The estimated phi0 cycles per flux ramp cycle
        s = self.flux_mod2(band, df, sync, channel=channel)

        new_fraction_full_scale = start_fraction_full_scale * n_phi0 / s

        # Amplitude must be less than 1
        if new_fraction_full_scale >= 1:
            self.log('Estimated fraction full scale too high: '+
                     f'{new_fraction_full_scale:2.4f}')
            self.log('Returning original value.')
            return start_fraction_full_scale
        else:
            return new_fraction_full_scale


    def flux_mod2(self, band, df, sync, min_scale=0, make_plot=False,
            channel=None, threshold=.5):
        """
        Attempts to find the number of phi0s in a tracking_setup.
        Takes df and sync from a tracking_setup with feedback off.
        Args:
        -----
        band (int) : which band
        df (float array): The df term from tracking setup with
            feedback off.
        sync (float array): The sync term from tracking setup.
        Opt Args:
        ---------
        min_scale (float): The minimum df amplitude used in analysis.
            This is used to cut channels that are not responding
            to flux ramp. Default is .002
        threshold (float): The minimum convolution amplitude to
            consider a peak. Default is .01
        make_plot (bool): Whether to make a plot. If True, you must
            also supply the channels to plot using the channel opt
            arg.
        channel (int or int array): The channels to plot. Default
            is None.
        Ret:
        ----
        n (float): The number of phi0 swept out per sync. To get
           lms_freq_hz, multiply by the flux ramp frequency.
        """
        sync_flag = self.make_sync_flag(sync)

        # The longest time between resets
        max_len = np.max(np.diff(sync_flag))
        n_sync = len(sync_flag) - 1
        n_samp, n_chan = np.shape(df)

        # Only for plotting
        channel = np.ravel(np.array(channel))

        peaks = np.zeros(n_chan)*np.nan

        band_chans=list(self.which_on(band))
        for ch in np.arange(n_chan):
            if ch in band_chans and np.std(df[:,ch]) > min_scale:
                # Holds the data for all flux ramps
                flux_resp = np.zeros((n_sync, max_len)) * np.nan
                for i in np.arange(n_sync):
                    flux_resp[i] = df[sync_flag[i]:sync_flag[i+1],ch]

                # Average over all the flux ramp sweeps to generate template
                template = np.nanmean(flux_resp, axis=0)
                template_mean = np.mean(template)
                template -= template_mean

                # Normalize template so thresholding can be
                # independent of the units of this crap
                if np.std(template)!=0:
                    template = template/np.std(template)

                # Multiply the matrix with the first element, then array
                # of first two elements, then array of first three...etc...
                # The array that maximizes this tells you the frequency
                corr_amp = np.zeros(max_len//2)
                for i in np.arange(1, max_len//2):
                    x = np.tile(template[:i], max_len//i+1)
                    # Normalize by elements in sum so that the
                    # correlation comes out something like unity at
                    # max
                    corr_amp[i] = np.sum(x[:max_len]*template)/max_len

                s, e = self.find_flag_blocks(corr_amp > threshold, min_gap=4)
                if len(s) == 0:
                    peaks[ch] = np.nan
                elif s[0] == e[0]:
                    peaks[ch] = s[0]
                else:
                    peaks[ch] = np.argmax(corr_amp[s[0]:e[0]]) + s[0]

                    #polyfit
                    Xf = [-1, 0, 1]
                    Yf = [corr_amp[int(peaks[ch]-1)],corr_amp[int(peaks[ch])],corr_amp[int(peaks[ch]+1)]]
                    V = np.polyfit(Xf, Yf, 2)
                    offset = -V[1]/(2.0 * V[0])
                    peak = offset + peaks[ch]

                    #kill shawn
                    peaks[ch]=peak

                if make_plot and ch in channel:
                    fig, ax = plt.subplots(2)
                    for i in np.arange(n_sync):
                        ax[0].plot(df[sync_flag[i]:sync_flag[i+1],ch]-
                                   template_mean)
                    ax[0].plot(template, color='k')
                    ax[1].plot(corr_amp)
                    ax[1].plot(peaks[ch], corr_amp[int(peaks[ch])], 'x' ,
                        color='k')

        return max_len/np.nanmedian(peaks)


    def make_sync_flag(self, sync):
        """
        Takes the sync from tracking setup and makes a flag for when the sync
        is True.
        Args:
        -----
        sync (float array): The sync term from tracking_setup
        Ret:
        ----
        start (int array): The start index of the sync
        end (int array) The end index of the sync
        """
        s, e = self.find_flag_blocks(sync[:,0], min_gap=1000)
        n_proc=self.get_number_processed_channels()
        return s//n_proc


    def flux_mod(self, df, sync, threshold=.4, minscale=.02,
                 min_spectrum=.9, make_plot=False):
        """
        Joe made this
        """
        mkr_ratio = 512  # ratio of rates on marker to rates in data
        num_channels = len(df[0,:])

        result = np.zeros(num_channels)
        peak_to_peak = np.zeros(num_channels)
        lp_power = np.zeros(num_channels)

        # Flux ramp markers
        n_sync = len(sync[:,0])
        mkrgap = 0
        mkr1 = 0
        mkr2 = 0
        totmkr = 0
        for n in np.arange(n_sync):
            mkrgap = mkrgap + 1
            if (sync[n,0] > 0) and (mkrgap > 1000):

                mkrgap = 0
                totmkr = totmkr + 1

                if mkr1 == 0:
                    mkr1 = n
                elif mkr2 == 0:
                    mkr2 = n

        dn = int(np.round((mkr2 - mkr1)/mkr_ratio))
        sn = int(np.round(mkr1/mkr_ratio))

        for ch in np.arange(num_channels):
            peak_to_peak[ch] = np.max(df[:,ch]) - np.min(df[:,ch])
            if peak_to_peak[ch] > 0:
                lp_power[ch] = np.std(df[:,ch])/np.std(np.diff(df[:,ch]))
            if ((peak_to_peak[ch] > minscale) and (lp_power[ch] > min_spectrum)):
                flux = np.zeros(dn)
                for mkr in np.arange(totmkr-1):
                    for n in np.arange(dn):
                        flux[n] = flux[n] + df[sn + mkr * dn + n, ch]
                flux = flux - np.mean(flux)

                sxarray = np.array([0])
                pts = len(flux)

                for rlen in np.arange(1, np.round(pts/2), dtype=int):
                    refsig = flux[:rlen]
                    sx = 0
                    for pt in np.arange(pts):
                        pr = pt % rlen
                        sx = sx + refsig[pr] * flux[pt]
                    sxarray = np.append(sxarray, sx)

                ac = 0
                for n in np.arange(pts):
                    ac = ac + flux[n]**2

                scaled_array = sxarray / ac

                pk = 0
                for n in np.arange(np.round(pts/2)-1, dtype=int):
                    if scaled_array[n] > threshold:
                        if scaled_array[n] > scaled_array[pk]:
                            pk = n
                        else:
                            break

                Xf = [-1, 0, 1]
                Yf = [scaled_array[pk-1], scaled_array[pk], scaled_array[pk+1]]
                V = np.polyfit(Xf, Yf, 2)
                offset = -V[1]/(2 * V[0])
                peak = offset + pk

                result[ch] = dn /  peak

                if make_plot:   # plotting routine to show sin fit
                    rs = 0
                    rc = 0
                    r = pts * [0]
                    s = pts * [0]
                    c = pts * [0]
                    scl = np.max(flux) - np.min(flux)
                    for n in range(0, pts):
                        s[n] = np.sin(n * 2 * np.pi / (dn/result[ch]))
                        c[n] = np.cos(n * 2 * np.pi / (dn/result[ch]))
                        rs = rs + s[n] * flux[n]
                        rc = rc + c[n] * flux[n]

                    theta = np.arctan2(rc, rs)
                    for n in range(0, pts):
                        r[n] = 0.5 * scl *  np.sin(theta + n * 2 * np.pi / (dn/result[ch]))

                    plt.figure()
                    plt.plot(r)
                    plt.plot(flux)
                    plt.show()

        fmod_array = []
        for n in range(0, num_channels):
            if(result[n] > 0):
                fmod_array.append(result[n])
        mod_median = np.median(fmod_array)
        return mod_median


    def dump_state(self, output_file=None, return_screen=False):
        """
        Dump the current tuning info to config file and write to disk
        Args:
        -----
        output_file (str): path to output file location. Defaults to the config
            file status dir and timestamp
        return_screen (bool): whether to also return the contents of the config
            file in addition to writing to file. Defaults False.
        """

        # get the HEMT info because why not
        self.add_output('hemt_status', self.get_amplifier_biases())

        # get jesd status for good measure
        for bay in self.bays:
            self.add_output('jesd_status bay ' + str(bay), self.check_jesd(bay))

        # get TES bias info
        self.add_output('tes_biases', list(self.get_tes_bias_bipolar_array()))

        # get flux ramp info
        self.add_output('flux_ramp_rate', self.get_flux_ramp_freq())
        self.add_output('flux_ramp_amplitude', self.get_fraction_full_scale()) # this doesn't exist yet

        # initialize band outputs
        self.add_output('band_outputs', {})

        # there is probably a better way to do this
        for band in self.config.get('init')['bands']:
            band_outputs = {} # Python copying is weird so this is easier
            band_outputs[band] = {}
            band_outputs[band]['amplitudes'] = list(self.get_amplitude_scale_array(band).astype(float))
            band_outputs[band]['freqs'] = list(self.get_center_frequency_array(band))
            band_outputs[band]['eta_mag'] = list(self.get_eta_mag_array(band))
            band_outputs[band]['eta_phase'] = list(self.get_eta_phase_array(band))
        self.config.update_subkey('outputs', 'band_outputs', band_outputs)

        # dump to file
        if output_file is None:
            filename = self.get_timestamp() + '_status.cfg'
            status_dir = self.status_dir
            output_file = os.path.join(status_dir, filename)

        self.log('Dumping status to file:{}'.format(output_file))
        self.write_output(output_file)

        if return_screen:
            return self.config.config


    def fake_resonance_dict(self, freqs, save_sweeps=False):
        """
        Takes a list of resonance frequencies and fakes a resonance dictionary
        so that we can run setup_notches on a subset without find_freqs
        Args:
        freqs (list of floats): given in MHz, list of frequencies to tune.
        Need to be within 100kHz to be really effective
        bands (list): band numbers that we have (This should be in the config
        file but I can't find it...)
        save_sweeps (bool): whether to save each band as an amplitude sweep.
        Defaults False.
        Outputs:
        resonance dictionary like the one that comes out of find_freqs
        You probably want to assign it to the right place, as in S.freq_resp =
        S.fake_resonance_dict(freqs, bands)
        """

        bands = self.config.get('init').get('bands')
        band_centers = []
        for band_no in bands: # we can get up to 8 bands I guess
            center = self.get_band_center_mhz(band_no)
            band_centers.append([band_no, center])

        band_nos = []
        for freq in freqs:
            freq_band = self.freq_to_band(freq, band_centers)
            band_nos.append(freq_band)

        # it's easier to work with np arrays
        freqs = np.asarray(freqs)
        band_nos = np.asarray(band_nos)

        freq_dict = {}
        for band in np.unique(band_nos):
            band_freqs = freqs[np.where(band_nos == band)]
            subband_freqs = []
            for f in band_freqs:
                (subband, foff) = self.freq_to_subband(band, f)
                subband_freqs.append(subband)

            freq_dict[band]={}
            freq_dict[band]['find_freq'] = {}
            freq_dict[band]['find_freq']['subband'] = subband_freqs
            freq_dict[band]['find_freq']['f'] = None
            freq_dict[band]['find_freq']['resp'] = None
            timestamp = self.get_timestamp()
            freq_dict[band]['timestamp'] = timestamp
            freq_dict[band]['find_freq']['resonance'] = freqs - \
                self.get_band_center_mhz(band)

            # do we want to save? default will be false
            if save_sweeps:
                save_name = '{}_amp_sweep_b{}_{}.txt'

                path = os.path.join(self.output_dir,
                    save_name.format(timestamp, str(band),'resonance'))
                np.savetxt(path, freq_dict[band]['find_freq']['resonance'])
                self.pub.register_file(path, 'resonances', format='txt')

        return freq_dict

    def freq_to_band(self, frequency, band_center_list):
        """
        Convert the frequency to which band we're in. This is almost certainly
        a duplicate but I can't find the original...
        Args:
        -----
        frequency (float): frequency in MHz
        band_center_list (list): frequency centers of bands we're running with.
        Formatted as [[band_no, band_center],[band_no, band_center],etc.]
        Ret:
        ----
        band_no of the frequency
        """

        band_width = 500. # hardcoding this is probably bad
        band_no = None # initialize this

        for center in band_center_list:
            center_low = center[1] - band_width/2.
            center_high = center[1] + band_width/2.
            if (center_low <= frequency <= center_high):
                band_no = center[0]
            else:
                continue

        if band_no is not None:
            return band_no
        else:
            print("Frequency not found. Check band list and that frequency "+
                "is given in MHz")
            return
