import logging
from typing import Optional
from collections import OrderedDict

import torch
from torch import nn

from ocf_datapipes.utils.consts import BatchKey

import pvnet
from pvnet.models.base_model import BaseModel
from pvnet.models.conv3d.encoders import AbstractNWPSatelliteEncoder
from pvnet.models.conv3d.dense_networks import AbstractTabularNetwork
from pvnet.models.conv3d.basic_blocks import ImageEmbedding, CompleteDropoutNd
from pvnet.optimizers import AbstractOptimizer



class Model(BaseModel):
    """
    Neural network which combines information from different sources.

    Architecture is roughly as follows:

    - Satellite data is put through an encoder which transforms it from 4D, with time, channel,
        heigh and width dimensions to become a 1D feature vector.
    - NWP, if included, is put through a similar encoder.
    - The satellite data, NWP data*, GSP history*, GSP ID embedding*, and sun paramters* are 
        concatenated into a 1D feature vector and passed through another neural network to combine
        them and produce a forecast. 
        
    * if included
    
    Args:
        image_encoder: Pytorch Module class used to encode the satellite (and NWP) data from 4D into
            an 1D feature vector.
        encoder_out_features: Number of features of the 1D vector created by the 
            `encoder_out_features` class.
        encoder_kwargs: Dictionary of optional kwargs for the `image_encoder` module.
        output_network: Pytorch Module class used to combine the 1D features to produce the 
            forecast.
        output_network_kwrgs: Dictionary of optional kwargs for the `output_network` module.
        include_sat: Include satellite data.
        include_nwp: Include NWP data.
        add_image_embedding_channel: Add a channel to the NWP and satellite data with the embedding
            of the GSP ID.
        include_gsp_yield_history: Include GSP yield data.
        include_sun: Include sun azimuth and altitude data.
        embedding_dim: Number of embedding dimensions to use for GSP ID. Not included if set to
            `None`.
        forecast_len: The amount of minutes that should be forecasted.
        history_len: The default amount of historical minutes that are used.
        sat_history_minutes: Period of historical data to use for satellite data. Defaults to 
            `history_len` if not provided.
        nwp_forecast_minutes: Period of future NWP forecast data to use. Defaults to  `forecast_len` 
            if not provided.
        nwp_history_minutes: Period of historical data to use for NWP data. Defaults to  
            `history_len` if not provided.
        sat_image_size_pixels: Image size (assumed square) of the satellite data.
        nwp_image_size_pixels: Image size (assumed square) of the NWP data.
        number_sat_channels: Number of satellite channels used.
        number_nwp_channels: Number of NWP channels used.
    """

    name = "conv3d_sat_nwp"

    
    def __init__(
        self,
        image_encoder: AbstractNWPSatelliteEncoder = pvnet.models.conv3d.encoders.DefaultPVNet,
        encoder_out_features: int = 128,
        encoder_kwargs: dict = dict(),
        
        output_network: AbstractTabularNetwork = pvnet.models.conv3d.dense_networks.DefaultFCNet,
        output_network_kwargs: dict = dict(),
        
        include_sat: bool = True,
        include_nwp: bool = True,
        add_image_embedding_channel: bool = False,
        include_gsp_yield_history: bool = True,
        include_sun: bool = True,
        embedding_dim: Optional[int] = 16,
        
        forecast_minutes: int = 30,
        history_minutes: int = 60,
        sat_history_minutes: Optional[int] = None,
        nwp_forecast_minutes: Optional[int] = None,
        nwp_history_minutes: Optional[int] = None,
        sat_image_size_pixels: int = 64,
        nwp_image_size_pixels: int = 64,
        number_sat_channels: int = 12,
        number_nwp_channels: int = 10,
        
        source_dropout=0.,
        cat_dropout=0.,
        
        optimizer: AbstractOptimizer = pvnet.optimizers.Adam(),
    ):

        self.include_gsp_yield_history = include_gsp_yield_history
        self.include_sat = include_sat
        self.include_nwp = include_nwp
        self.include_sun = include_sun
        self.embedding_dim = embedding_dim
        self.add_image_embedding_channel = add_image_embedding_channel
        
        super().__init__(history_minutes, forecast_minutes, optimizer)
        
        if include_sat:
            # TODO: remove this hardcoding
            # We limit the history to have a delay of 15 mins in satellite data
            if sat_history_minutes is None: sat_history_minutes = history_minutes
            self.sat_sequence_len = sat_history_minutes//5 + 1 - 3
            
            self.sat_encoder = image_encoder(
                sequence_length=self.sat_sequence_len,
                image_size_pixels=sat_image_size_pixels,
                in_channels=number_sat_channels+add_image_embedding_channel,
                out_features=encoder_out_features,
                **encoder_kwargs,
            )
            if add_image_embedding_channel:
                self.sat_embed = ImageEmbedding(318, sat_image_size_pixels, self.sat_sequence_len)

        if include_nwp:
            if nwp_history_minutes is None: nwp_history_minutes = history_minutes
            if nwp_forecast_minutes is None: nwp_forecast_minutes = forecast_minutes
            nwp_sequence_len = nwp_history_minutes//60 + nwp_forecast_minutes//60 + 1
            
            self.nwp_encoder = image_encoder(
                sequence_length=nwp_sequence_len,
                image_size_pixels=nwp_image_size_pixels,
                in_channels=number_nwp_channels+add_image_embedding_channel,
                out_features=encoder_out_features,
                **encoder_kwargs,
            )
            if add_image_embedding_channel:
                self.nwp_embed = ImageEmbedding(318, nwp_image_size_pixels, nwp_sequence_len)

        if self.embedding_dim:
            self.embed = nn.Embedding(
                num_embeddings=318, embedding_dim=self.embedding_dim
            )

        if self.include_sun:
            # the minus 12 is bit of hard coded smudge for pvnet
            self.sun_fc1 = nn.Linear(
                in_features=2 * (self.forecast_len_30 + self.history_len_30 + 1),
                out_features=16,
            )

        fc_in_features = 0
        if include_sat:
            fc_in_features += encoder_out_features
        if include_nwp:
            fc_in_features += encoder_out_features
        if include_gsp_yield_history:
            fc_in_features += self.history_len_30
        if embedding_dim:
            fc_in_features += embedding_dim
        if include_sun:
            fc_in_features += 16
    
        self.output_network = nn.Sequential(
            nn.Dropout(cat_dropout),
            output_network(
                in_features=fc_in_features,
                out_features=self.forecast_len,
                **output_network_kwargs,
            )
        )
        
        self.source_dropout_0d = CompleteDropoutNd(0, p=source_dropout)
        self.source_dropout_3d = CompleteDropoutNd(3, p=source_dropout)
        
        self.save_hyperparameters()


    def forward(self, x):
        
        modes = OrderedDict()
        # ******************* Satellite imagery *************************
        if self.include_sat:
            # Shape: batch_size, seq_length, channel, height, width
            sat_data = x[BatchKey.satellite_actual]
            sat_data = torch.swapaxes(sat_data, 1, 2).float() # switch time and channels
            sat_data = sat_data[:, :, : self.sat_sequence_len]
            if self.add_image_embedding_channel:
                id = x[BatchKey.gsp_id][:, 0].int()
                sat_data = self.sat_embed(sat_data, id)
            sat_data = self.source_dropout_3d(sat_data)
            modes["sat"] = self.sat_encoder(sat_data)

        # *********************** NWP Data ************************************
        if self.include_nwp:
            # shape: batch_size, seq_len, n_chans, height, width
            nwp_data = x[BatchKey.nwp].float()
            nwp_data = torch.swapaxes(nwp_data, 1, 2) # switch time and channels
            if self.add_image_embedding_channel:
                id = x[BatchKey.gsp_id][:, 0].int()
                nwp_data = self.nwp_embed(nwp_data, id)
            nwp_data = self.source_dropout_3d(nwp_data)
            modes["nwp"] = self.nwp_encoder(nwp_data)
            
        # *********************** GSP Data ************************************
        # add gsp yield history
        if self.include_gsp_yield_history:
            gsp_history = x[BatchKey.gsp][:, : self.history_len_30].float()
            gsp_history = gsp_history.reshape(gsp_history.shape[0], -1)
            gsp_history = self.source_dropout_0d(gsp_history)
            modes["gsp"] = gsp_history

        # ********************** Embedding of GSP ID ********************
        if self.embedding_dim:
            id = x[BatchKey.gsp_id][:, 0].int()
            id_embedding = self.embed(id)
            modes["id"] = id_embedding

        if self.include_sun:
            sun = torch.cat(
                (x[BatchKey.gsp_solar_azimuth], x[BatchKey.gsp_solar_elevation]), 
                dim=1
            ).float()
            sun = self.source_dropout_0d(sun)
            sun = self.sun_fc1(sun)
            modes["sun"] = sun_out
         
        out = self.output_network(modes)

        return out
    
    
if __name__=="__main__":
    from torch.optim import SGD
    history = 60
    forecast = 30

    sun_in = torch.rand((3, (history+forecast)//30+1)) #0D
    gsp_id = torch.randint(1, 317, (3,1))
    # Shape: batch_size, seq_length, channel, height, width
    sat_data = torch.rand((3, history//5 +1 - 3, 11, 24, 24)) #3D
    # shape: batch_size, seq_len, n_chans, height, width
    nwp_data = torch.rand((3, (history+forecast)//60+1, 2, 24, 24)) #3D
    gsp = torch.rand((3, (history+forecast)//30+1)) #0D

    batch = {
        BatchKey.gsp_solar_azimuth: sun_in,
        BatchKey.gsp_solar_elevation: sun_in,
        BatchKey.gsp_id: gsp_id,
        # Shape: batch_size, seq_length, channel, height, width
        BatchKey.satellite_actual: sat_data,
        BatchKey.nwp: nwp_data,
        BatchKey.gsp: gsp,
    }


    model = Model(
        image_encoder = pvnet.models.conv3d.encoders.DefaultPVNet,
        encoder_kwargs = dict(),
        #image_encoder = pvnet.models.conv3d.encoders.EncoderUNET,
        #encoder_kwargs = dict(n_downscale=3),
        
        output_network = pvnet.models.conv3d.dense_networks.DefaultFCNet,
        output_network_kwargs = dict(),
        #output_network = pvnet.models.conv3d.dense_networks.ResFCNet,
        #output_network_kwargs = dict(),
        
        include_gsp_yield_history = True,
        include_sat = True,
        include_nwp = True,
        add_image_embedding_channel=True,
        forecast_minutes = 30,
        history_minutes = 60,
        sat_history_minutes = None,
        nwp_forecast_minutes = None,
        nwp_history_minutes = None,
        sat_image_size_pixels = 24,
        nwp_image_size_pixels = 24,
        number_sat_channels = 11,
        number_nwp_channels = 2,
        encoder_out_features = 64,
        embedding_dim = 16,
        include_sun = True,
    )
    
    opt = SGD(model.parameters(), lr=0.001)
    
    #print(model)
    print(model(batch))
    model(batch).sum().backward()
    