"""The default composite model architecture for PVNet"""

from collections import OrderedDict
from typing import Optional
import glob

import torch
from ocf_datapipes.batch import BatchKey, NWPBatchKey
from torch import nn
import torch.nn.functional as F
import hydra

import pvnet
from pvnet.models.base_model import BaseModel
from pvnet.models.multimodal.basic_blocks import ImageEmbedding
from pvnet.models.multimodal.encoders.basic_blocks import AbstractNWPSatelliteEncoder
from pvnet.models.multimodal.linear_networks.basic_blocks import AbstractLinearNetwork
from pvnet.models.multimodal.site_encoders.basic_blocks import AbstractPVSitesEncoder
from pvnet.optimizers import AbstractOptimizer
from pyaml_env import parse_config


from torchvision.transforms.functional import center_crop

class Model(BaseModel):
    """Neural network which combines information from different sources

    Architecture is roughly as follows:

    - Satellite data, if included, is put through an encoder which transforms it from 4D, with time,
        channel, height, and width dimensions to become a 1D feature vector.
    - NWP, if included, is put through a similar encoder.
    - PV site-level data, if included, is put through an encoder which transforms it from 2D, with
        time and system-ID dimensions, to become a 1D feature vector.
    - The satellite features*, NWP features*, PV site-level features*, GSP ID embedding*, and sun
        paramters* are concatenated into a 1D feature vector and passed through another neural
        network to combine them and produce a forecast.

    * if included
    """

    name = "unimodal_teacher"

    def __init__(
        self,
        output_network: AbstractLinearNetwork,
        output_quantiles: Optional[list[float]] = None,
        include_gsp_yield_history: bool = True,
        include_sun: bool = True,
        embedding_dim: Optional[int] = 16,
        forecast_minutes: int = 30,
        history_minutes: int = 60,
        optimizer: AbstractOptimizer = pvnet.optimizers.Adam(),
        target_key: str = "gsp",
        mode_teacher_dict: dict = {},
        val_best: bool = True,
        cold_start: bool = True,
        enc_loss_frac: float = 0.3,
    ):
        """Neural network which combines information from different sources.

        Notes:
            In the args, where it says a module `m` is partially instantiated, it means that a
            normal pytorch module will be returned by running `mod = m(**kwargs)`. In this library,
            this partial instantiation is generally achieved using partial instantiation via hydra.
            However, the arg is still valid as long as `m(**kwargs)` returns a valid pytorch module
            - for example if `m` is a regular function.

        Args:
            output_network: A partially instatiated pytorch Module class used to combine the 1D
                features to produce the forecast.
            output_quantiles: A list of float (0.0, 1.0) quantiles to predict values for. If set to
                None the output is a single value.
            include_gsp_yield_history: Include GSP yield data.
            include_sun: Include sun azimuth and altitude data.
            embedding_dim: Number of embedding dimensions to use for GSP ID. Not included if set to
                `None`.
            forecast_minutes: The amount of minutes that should be forecasted.
            history_minutes: The default amount of historical minutes that are used.
            optimizer: Optimizer factory function used for network.
            target_key: The key of the target variable in the batch.
            cold_start: Whether to train the uni-modal encoders from scratch. Else start them with
                weights from the uni-modal teachers.
        """

        self.include_gsp_yield_history = include_gsp_yield_history
        self.include_sun = include_sun
        self.embedding_dim = embedding_dim
        self.target_key_name = target_key
        self.enc_loss_frac = enc_loss_frac
        self.include_sat = False
        self.include_nwp = False
        self.include_pv = False
        self.add_image_embedding_channel = False

        super().__init__(
            history_minutes=history_minutes,
            forecast_minutes=forecast_minutes,
            optimizer=optimizer,
            output_quantiles=output_quantiles,
            target_key=BatchKey.gsp if target_key == "gsp" else BatchKey.pv,
        )
        
        self.gsp_len = (self.forecast_len_30 + self.history_len_30 + 1)


        # Number of features expected by the output_network
        # Add to this as network pices are constructed
        fusion_input_features = 0
        
        self.teacher_models = torch.nn.ModuleDict()
        
        
        for mode, path in mode_teacher_dict.items():
        
            # load teacher model and freeze its weights
            self.teacher_models[mode] = self.get_unimodal_encoder(path, True, val_best=val_best)
            
            for param in self.teacher_models[mode].parameters():
                param.requires_grad = False
            
            # Recreate model as student
            mode_student_model = self.get_unimodal_encoder(
                path, load_weights=(not cold_start), val_best=val_best
            )
            
            if mode=="sat":
                self.include_sat = True
                self.sat_sequence_len = mode_student_model.sat_sequence_len
                self.sat_encoder = mode_student_model.sat_encoder
                
                if mode_student_model.add_image_embedding_channel:
                    self.sat_embed = mode_student_model.sat_embed
                    self.add_image_embedding_channel = True

                fusion_input_features += self.sat_encoder.out_features
                
            
            elif mode=="pv":
                self.include_pv = True
                self.pv_encoder = mode_student_model.pv_encoder
                fusion_input_features += self.pv_encoder.out_features
                
            
            elif mode.startswith("nwp"):
                nwp_source = mode.removeprefix("nwp/")
                
                if not self.include_nwp:
                    self.include_nwp = True
                    self.nwp_encoders_dict = torch.nn.ModuleDict()
                    
                    if mode_student_model.add_image_embedding_channel:
                        self.add_image_embedding_channel = True
                        self.nwp_embed_dict = torch.nn.ModuleDict()

                self.nwp_encoders_dict[nwp_source] = (
                    mode_student_model.nwp_encoders_dict[nwp_source]
                )
                
                if self.add_image_embedding_channel:
                    self.nwp_embed_dict[nwp_source] = mode_student_model.nwp_embed_dict[nwp_source]

                fusion_input_features += self.nwp_encoders_dict[nwp_source].out_features
            
        
        if self.embedding_dim:
            self.embed = nn.Embedding(num_embeddings=318, embedding_dim=embedding_dim)
            fusion_input_features += embedding_dim

        if self.include_sun:
            self.sun_fc1 = nn.Linear(
                in_features=2 * self.gsp_len,
                out_features=16,
            )
            fusion_input_features += 16

        if include_gsp_yield_history:
            fusion_input_features += self.history_len_30

        self.output_network = output_network(
            in_features=fusion_input_features,
            out_features=self.num_output_features,
        )

        self.save_hyperparameters()
        
    
    def get_unimodal_encoder(self, path, load_weights, val_best):
        
        model_config = parse_config(f"{path}/model_config.yaml")

        # Load the teacher model
        encoder = hydra.utils.instantiate(model_config)
        
        if load_weights:
            if val_best:
                # Only one epoch (best) saved per model
                files = glob.glob(f"{path}/epoch*.ckpt")
                assert len(files) == 1
                checkpoint = torch.load(files[0], map_location="cpu")
            else:
                checkpoint = torch.load(f"{path}/last.ckpt", map_location="cpu")

            encoder.load_state_dict(state_dict=checkpoint["state_dict"])
        return encoder
    
        
    def teacher_forward(self, x):
        
        modes = OrderedDict()
        for mode, teacher_model in self.teacher_models.items():
        
            # ******************* Satellite imagery *************************
            if mode=="sat":
                # Shape: batch_size, seq_length, channel, height, width
                sat_data = x[BatchKey.satellite_actual][:, : teacher_model.sat_sequence_len]
                sat_data = torch.swapaxes(sat_data, 1, 2).float()  # switch time and channels

                sat_data = center_crop(
                    sat_data, output_size=teacher_model.sat_encoder.image_size_pixels
                ) 

                if self.add_image_embedding_channel:
                    id = x[BatchKey.gsp_id][:, 0].int()
                    sat_data = teacher_model.sat_embed(sat_data, id)
                
                modes[mode] = teacher_model.sat_encoder(sat_data)

            # *********************** NWP Data ************************************
            if mode.startswith("nwp"):
                nwp_source = mode.removeprefix("nwp/")
                
                # shape: batch_size, seq_len, n_chans, height, width
                nwp_data = x[BatchKey.nwp][nwp_source][NWPBatchKey.nwp].float()
                nwp_data = torch.swapaxes(nwp_data, 1, 2)  # switch time and channels
                nwp_data = torch.clip(nwp_data, min=-50, max=50)
                nwp_data = center_crop(
                    nwp_data, 
                    output_size=teacher_model.nwp_encoders_dict[nwp_source].image_size_pixels
                ) 
                nwp_data = nwp_data[:,:,:teacher_model.nwp_encoders_dict[nwp_source].sequence_length]
                if teacher_model.add_image_embedding_channel:
                    id = x[BatchKey.gsp_id][:, 0].int()
                    nwp_data = teacher_model.nwp_embed_dict[nwp_source](nwp_data, id)

                nwp_out = teacher_model.nwp_encoders_dict[nwp_source](nwp_data)
                modes[mode] = nwp_out

            # *********************** PV Data *************************************
            # Add site-level PV yield
            if mode=="pv":
                modes[mode] = teacher_model.pv_encoder(x)
                
        return modes

    
    def forward(self, x, return_modes=False):
        """Run model forward"""
        
        x[BatchKey.gsp] = x[BatchKey.gsp][:, :self.gsp_len]
        x[BatchKey.gsp_time_utc] = x[BatchKey.gsp_time_utc][:, :self.gsp_len]

        
        modes = OrderedDict()
        # ******************* Satellite imagery *************************
        if self.include_sat:
            # Shape: batch_size, seq_length, channel, height, width
            sat_data = x[BatchKey.satellite_actual][:, : self.sat_sequence_len]
            sat_data = torch.swapaxes(sat_data, 1, 2).float()  # switch time and channels
            
            sat_data = center_crop(sat_data, output_size=self.sat_encoder.image_size_pixels) 
            
            if self.add_image_embedding_channel:
                id = x[BatchKey.gsp_id][:, 0].int()
                sat_data = self.sat_embed(sat_data, id)
            modes["sat"] = self.sat_encoder(sat_data)

        # *********************** NWP Data ************************************
        if self.include_nwp:
            # Loop through potentially many NMPs
            for nwp_source in self.nwp_encoders_dict:
                # shape: batch_size, seq_len, n_chans, height, width
                nwp_data = x[BatchKey.nwp][nwp_source][NWPBatchKey.nwp].float()
                nwp_data = torch.swapaxes(nwp_data, 1, 2)  # switch time and channels
                nwp_data = torch.clip(nwp_data, min=-50, max=50)
                nwp_data = center_crop(
                    nwp_data, 
                    output_size=self.nwp_encoders_dict[nwp_source].image_size_pixels
                ) 
                nwp_data = nwp_data[:,:,:self.nwp_encoders_dict[nwp_source].sequence_length]
                if self.add_image_embedding_channel:
                    id = x[BatchKey.gsp_id][:, 0].int()
                    nwp_data = self.nwp_embed_dict[nwp_source](nwp_data, id)
                
                nwp_out = self.nwp_encoders_dict[nwp_source](nwp_data)
                modes[f"nwp/{nwp_source}"] = nwp_out

        # *********************** PV Data *************************************
        # Add site-level PV yield
        if self.include_pv:
            if self.target_key_name != "pv":
                modes["pv"] = self.pv_encoder(x)
            else:
                # Target is PV, so only take the history
                pv_history = x[BatchKey.pv][:, : self.history_len_30].float()
                modes["pv"] = self.pv_encoder(pv_history)

        # *********************** GSP Data ************************************
        # add gsp yield history
        if self.include_gsp_yield_history:
            gsp_history = x[BatchKey.gsp][:, : self.history_len_30].float()
            gsp_history = gsp_history.reshape(gsp_history.shape[0], -1)
            modes["gsp"] = gsp_history

        # ********************** Embedding of GSP ID ********************
        if self.embedding_dim:
            id = x[BatchKey.gsp_id][:, 0].int()
            id_embedding = self.embed(id)
            modes["id"] = id_embedding

        if self.include_sun:
            
            sun = torch.cat(
                (
                    x[BatchKey.gsp_solar_azimuth][:, :self.gsp_len], 
                    x[BatchKey.gsp_solar_elevation][:, :self.gsp_len]
                ),
                dim=1
            ).float()
            sun = self.sun_fc1(sun)
            modes["sun"] = sun

        out = self.output_network(modes)

        if self.use_quantile_regression:
            # Shape: batch_size, seq_length * num_quantiles
            out = out.reshape(out.shape[0], self.forecast_len_30, len(self.output_quantiles))

        if return_modes:
            return out, modes
        else:
            return out

        
    def _calculate_teacher_loss(self, modes, teacher_modes):
        enc_losses = {}
        for m, enc in teacher_modes.items():
            enc_losses[f"enc_loss/{m}"] = F.l1_loss(enc, modes[m])
        enc_losses["enc_loss/total"] = sum([v for k,v in enc_losses.items()])
        return enc_losses
        
        
    def training_step(self, batch, batch_idx):
        """Run training step"""
        y_hat, modes = self.forward(batch, return_modes=True)
        y = batch[self._target_key][:, -self.forecast_len_30 :, 0]
        
        losses = self._calculate_common_losses(y, y_hat)
        
        teacher_modes = self.teacher_forward(batch)
        teacher_loss = self._calculate_teacher_loss(modes, teacher_modes)
        losses.update(teacher_loss)

        if self.use_quantile_regression:
            opt_target = losses["quantile_loss"]
        else:
            opt_target = losses["MAE"]
            
        t_loss = teacher_loss["enc_loss/total"]
        
        # The scales of the two losses
        l_s = opt_target.detach()
        tl_s = max(t_loss.detach(), 1e-9)
        
        #opt_target = t_loss/tl_s * l_s * self.enc_loss_frac + opt_target * (1-self.enc_loss_frac)
        losses["opt_loss"]  = t_loss/tl_s * l_s * self.enc_loss_frac + opt_target * (1-self.enc_loss_frac)
    
        losses = {f"{k}/train": v for k, v in losses.items()}
        self._training_accumulate_log(batch, batch_idx, losses, y_hat)
        
        return losses["opt_loss/train"]

