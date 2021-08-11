from predict_pv_yield.models.perceiver_rnn import PerceiverRNN, params, TOTAL_SEQ_LEN
import numpy as np
import torch

def test_init_model():
    """ Initilize the model"""
    model = PerceiverRNN(history_len=3, forecast_len=3,nwp_channels=params['nwp_channels'])


def test_model_forward():

    model = PerceiverRNN(history_len=params['history_len'],
                         forecast_len=params['forecast_len'],
                         nwp_channels=params['nwp_channels']) # doesnt do anything

    # setup parameters TODO, should take this from a config file
    batch_size = params['batch_size']
    seq_length = TOTAL_SEQ_LEN
    width = 16 # this doesnt seem to matter
    height = 16 # this doesnt seem to matter
    channel = len(params['sat_channels'])
    nwp_channels = len(params['nwp_channels'])

    # set up fake data
    x = {}
    # satelite data
    x['sat_data'] = torch.randn(batch_size, seq_length, width, height, channel)

    # numerical weather predictions
    x['nwp'] = torch.randn(batch_size, nwp_channels, seq_length, 2, 2)

    # setup fake hour of the day, an dat of the year parameters
    for time_variable in ['hour_of_day_sin', 'hour_of_day_cos', 'day_of_year_sin', 'day_of_year_cos']:
        x[time_variable] = torch.randn(batch_size, seq_length)

    # pv yield data
    x['pv_yield'] = torch.randn(batch_size, seq_length)

    # run data through model
    y = model(x)

    # check out put is the correct shape
    assert len(y.shape) == 2
    assert y.shape[0] == batch_size
    assert y.shape[1] == params['forecast_len']


