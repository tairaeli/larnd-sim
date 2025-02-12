"""
Module that simulates the front-end electronics (triggering, ADC)
"""

import numpy as np
import h5py
import yaml

from numba import cuda
from numba.cuda.random import xoroshiro128p_normal_float32

from larpix.packet import Packet_v2, TimestampPacket, TriggerPacket
from larpix.packet import PacketCollection
from larpix.format import hdf5format
from tqdm import tqdm
from . import consts

#: Maximum number of ADC values stored per pixel
MAX_ADC_VALUES = 10
#: Discrimination threshold
DISCRIMINATION_THRESHOLD = 7e3*consts.e_charge
#: ADC hold delay in clock cycles
ADC_HOLD_DELAY = 15
#: Clock cycle time in :math:`\mu s`
CLOCK_CYCLE = 0.1
#: Front-end gain in :math:`mV/ke-`
GAIN = 4/1e3
#: Common-mode voltage in :math:`mV`
V_CM = 288
#: Reference voltage in :math:`mV`
V_REF = 1300
#: Pedestal voltage in :math:`mV`
V_PEDESTAL = 580
#: Number of ADC counts
ADC_COUNTS = 2**8
#: Reset noise in e-
RESET_NOISE_CHARGE = 900
#: Uncorrelated noise in e-
UNCORRELATED_NOISE_CHARGE = 500

import logging
logging.basicConfig()
logger = logging.getLogger('fee')
logger.setLevel(logging.WARNING)
logger.info("ELECTRONICS SIMULATION")

def rotate_tile(pixel_id, tile_id):
    axes = consts.tile_orientations[tile_id-1]
    x_axis = axes[2]
    y_axis = axes[1]

    pix_x = pixel_id[0]
    if x_axis < 0:
        pix_x = consts.n_pixels_per_tile[0]-pixel_id[0]-1

    pix_y = pixel_id[1]
    if y_axis < 0:
        pix_y = consts.n_pixels_per_tile[1]-pixel_id[1]-1

    return pix_x, pix_y

def export_to_hdf5(adc_list, adc_ticks_list, unique_pix, current_fractions, track_ids, filename, bad_channels=None):
    """
    Saves the ADC counts in the LArPix HDF5 format.
    Args:
        adc_list (:obj:`numpy.ndarray`): list of ADC values for each pixel
        adc_ticks_list (:obj:`numpy.ndarray`): list of time ticks for each pixel
        unique_pix (:obj:`numpy.ndarray`): list of pixel IDs
        current_fractions (:obj:`numpy.ndarray`): array containing the fractional current
            induced by each track on each pixel
        track_ids (:obj:`numpy.ndarray`): 2D array containing the track IDs associated
            to each pixel
        filename (str): filename of HDF5 output file
        bad_channels (dict): dictionary containing as value a list of bad channels and as
            the chip key
    Returns:
        tuple: a tuple containing the list of LArPix packets and the list of entries
            for the `mc_packets_assn` dataset
    """

    dtype = np.dtype([('track_ids','(%i,)i8' % track_ids.shape[2]), ('fraction', '(%i,)f8' % current_fractions.shape[2])])
    packets = [TimestampPacket()]
    packets_mc = [[-1]*track_ids.shape[2]]
    packets_frac = [[0]*current_fractions.shape[2]]
    packets_mc_ds = []
    last_event = -1

    if bad_channels:
        with open(bad_channels, 'r') as f:
            bad_channels_list = yaml.load(f, Loader=yaml.FullLoader)

    for itick, adcs in enumerate(tqdm(adc_list, desc="Writing to HDF5...")):
        ts = adc_ticks_list[itick]
        pixel_id = unique_pix[itick]

        plane_id = int(pixel_id[0] // consts.n_pixels[0])
        tile_x = int((pixel_id[0] - consts.n_pixels[0] * plane_id) // consts.n_pixels_per_tile[0])
        tile_y = int(pixel_id[1] // consts.n_pixels_per_tile[1])
        tile_id = consts.tile_map[plane_id][tile_x][tile_y]

        for iadc, adc in enumerate(adcs):
            t = ts[iadc]

            if adc > digitize(0):
                event = t // (consts.time_interval[1]*3)
                time_tick = int(np.floor(t/CLOCK_CYCLE))

                if event != last_event:
                    packets.append(TriggerPacket(io_group=1,trigger_type=b'\x02',timestamp=int(event*consts.time_interval[1]/consts.t_sampling*3)))
                    packets_mc.append([-1]*track_ids.shape[2])
                    packets_frac.append([0]*current_fractions.shape[2])
                    packets.append(TriggerPacket(io_group=2,trigger_type=b'\x02',timestamp=int(event*consts.time_interval[1]/consts.t_sampling*3)))
                    packets_mc.append([-1]*track_ids.shape[2])
                    packets_frac.append([0]*current_fractions.shape[2])
                    last_event = event

                p = Packet_v2()

                try:
                    chip, channel = consts.pixel_connection_dict[rotate_tile(pixel_id%[consts.n_pixels_per_tile[0],consts.n_pixels_per_tile[1]], tile_id)]
                except KeyError:
                    logger.warning("Pixel ID not valid", pixel_id)
                    continue

                p.dataword = int(adc)
                p.timestamp = time_tick

                try:
                    io_group_io_channel = consts.tile_chip_to_io[tile_id][chip]
                except KeyError:
                    logger.info("Chip %i on tile %i not found" % (chip, tile_id))
                    continue

                io_group, io_channel = io_group_io_channel // 1000, io_group_io_channel % 1000
                chip_key = "%i-%i-%i" % (io_group, io_channel, chip)

                if bad_channels:
                    if chip_key in bad_channels_list:
                        if channel in bad_channels_list[chip_key]:
                            logger.info("Channel %i on chip %s disabled" % (channel, chip_key))
                            continue

                p.chip_key = chip_key
                p.channel_id = channel
                p.packet_type = 0
                p.first_packet = 1
                p.assign_parity()

                packets_mc.append(track_ids[itick][iadc])
                packets_frac.append(current_fractions[itick][iadc])
                packets.append(p)
            else:
                break

    packet_list = PacketCollection(packets, read_id=0, message='')

    hdf5format.to_file(filename, packet_list)

    if packets:
        packets_mc_ds = np.empty(len(packets), dtype=dtype)
        packets_mc_ds['track_ids'] = packets_mc
        packets_mc_ds['fraction'] = packets_frac

    with h5py.File(filename, 'a') as f:
        if "mc_packets_assn" in f.keys():
            del f['mc_packets_assn']
        f.create_dataset("mc_packets_assn", data=packets_mc_ds)

        f['configs'].attrs['vdrift'] = consts.vdrift
        f['configs'].attrs['long_diff'] = consts.long_diff
        f['configs'].attrs['tran_diff'] = consts.tran_diff
        f['configs'].attrs['lifetime'] = consts.lifetime
        f['configs'].attrs['drift_length'] = consts.drift_length

    return packets, packets_mc_ds

def digitize(integral_list):
    """
    The function takes as input the integrated charge and returns the digitized
    ADC counts.

    Args:
        integral_list (:obj:`numpy.ndarray`): list of charge collected by each pixel

    Returns:
        :obj:`numpy.ndarray`: list of ADC values for each pixel
    """
    import cupy as cp
    xp = cp.get_array_module(integral_list)
    adcs = xp.minimum(xp.around(xp.maximum((integral_list*GAIN/consts.e_charge+V_PEDESTAL - V_CM), 0) \
                      * ADC_COUNTS/(V_REF-V_CM)), ADC_COUNTS)

    return adcs

@cuda.jit
def get_adc_values(pixels_signals,
                   pixels_signals_tracks,
                   time_ticks,
                   adc_list,
                   adc_ticks_list,
                   time_padding,
                   rng_states,
                   current_fractions):
    """
    Implementation of self-trigger logic

    Args:
        pixels_signals (:obj:`numpy.ndarray`): list of induced currents for
            each pixel
        pixels_signals_tracks (:obj:`numpy.ndarray`): list of induced currents
            for each track that induces current on each pixel
        time_ticks (:obj:`numpy.ndarray`): list of time ticks for each pixel
        adc_list (:obj:`numpy.ndarray`): list of integrated charges for each
            pixel
        adc_ticks_list (:obj:`numpy.ndarray`): list of the time ticks that
            correspond to each integrated charge
        time_padding (float): time interval to add to each time tick.
        rng_states (:obj:`numpy.ndarray`): array of random states for noise
            generation
        current_fractions (:obj:`numpy.ndarray`): 2D array that will contain
            the fraction of current induced on the pixel by each track
    """
    ip = cuda.grid(1)

    if ip < pixels_signals.shape[0]:
        curre = pixels_signals[ip]
        ic = 0
        iadc = 0
        q_sum = xoroshiro128p_normal_float32(rng_states, ip) * RESET_NOISE_CHARGE * consts.e_charge
#         integrate[ip][ic] = q_sum

        while ic < curre.shape[0]:

            if iadc >= MAX_ADC_VALUES:
                print("More ADC values than possible, ", MAX_ADC_VALUES)
                break

            q = curre[ic]*consts.t_sampling

            q_sum += q

            for itrk in range(current_fractions.shape[2]):
                current_fractions[ip][iadc][itrk] += pixels_signals_tracks[ip][ic][itrk]*consts.t_sampling
#             integrate[ip][ic] = q_sum

            q_noise = xoroshiro128p_normal_float32(rng_states, ip) * UNCORRELATED_NOISE_CHARGE * consts.e_charge

            if q_sum + q_noise >= DISCRIMINATION_THRESHOLD:
                crossing_time_tick = ic

                interval = round((3 * CLOCK_CYCLE + ADC_HOLD_DELAY * CLOCK_CYCLE) / consts.t_sampling)
                integrate_end = ic+interval

                ic+=1

                while ic <= integrate_end and ic < curre.shape[0]:
                    q = curre[ic] * consts.t_sampling
                    q_sum += q
                    for itrk in range(current_fractions.shape[2]):
                        current_fractions[ip][iadc][itrk] += pixels_signals_tracks[ip][ic][itrk]*consts.t_sampling
#                     integrate[ip][ic] = q_sum
                    ic+=1

                adc = q_sum + xoroshiro128p_normal_float32(rng_states, ip) * UNCORRELATED_NOISE_CHARGE * consts.e_charge

                if adc < DISCRIMINATION_THRESHOLD:
                    ic += round(CLOCK_CYCLE / consts.t_sampling)
                    q_sum = xoroshiro128p_normal_float32(rng_states, ip) * UNCORRELATED_NOISE_CHARGE * consts.e_charge
#                     integrate[ip][ic] = q_sum
                    for itrk in range(current_fractions.shape[2]):
                        current_fractions[ip][iadc][itrk] = 0
                    continue

                tot_backtracked = 0
                for itrk in range(current_fractions.shape[2]):
                    tot_backtracked += current_fractions[ip][iadc][itrk]

                for itrk in range(current_fractions.shape[2]):
                    current_fractions[ip][iadc][itrk] /= tot_backtracked

                adc_list[ip][iadc] = adc

                #+2-tick delay from when the PACMAN receives the trigger and when it registers it.
                adc_ticks_list[ip][iadc] = time_ticks[crossing_time_tick]+time_padding+2

                ic += round(CLOCK_CYCLE / consts.t_sampling)
                q_sum = xoroshiro128p_normal_float32(rng_states, ip) * RESET_NOISE_CHARGE * consts.e_charge
#                 integrate[ip][ic] = q_sum
                iadc += 1
                continue

            ic += 1
