"""
Code for running full linear ridge simulations.
"""
import numpy as np
import os

import aux
from db import make_session, d_models
from ntwk import LIFNtwk
from search import trial_to_p, trial_to_stable_ntwk


def run_smln(
        trial_id, d_model, pre, C, P, C_, save, seed, commit, cache_file=None):
    """
    Run a full simulation starting from either a replay-only or full
    trial.
    
    :param d_model: data model specifying which trial type/table
    :param C: param module lin_ridge.full_global
    :param C_: param module lin_ridge.search_global
    :param save: whether to save response in database
    :param seed: RNG seed to ensure repeatability
    :param commit: ID of current commit (to ensure repeatability)
    :param cache_file: temporary file containing recurrent ntwk cxn 
        matrices and replay triggering stimulus; if no cache_file is
        specified, look for the default one in the directory C.CACHE_DIR.
    """
    np.random.seed(seed)
    
    # load replay-only trial
    session = make_session()
    trial = session.query(d_model).get(trial_id)

    if trial.__class__.__name__ == 'LinRidgeFullTrial':
        # get replay-only trial
        trial = trial.lin_ridge_trial

    session.close()
    
    # get trial params
    p = trial_to_p(trial)

    if cache_file is None:
        # construct default cache file path
        base_name = 'ws_rcr_replay_trigger_lin_ridge_trial_{}.npy'.format(trial_id)
        cache_file = os.path.join(C.CACHE_DIR, base_name)
    
    # make new cache file if nonexistent
    if not os.path.exists(cache_file):
        print('No cache file found at {}.'.format(cache_file))
        
        print('Running replay-only smln and computing replay trigger...')
        
        # run replay-only trial to get stable ntwk and initial conditions
        ntwk, vs_0, gs_0, spks_forced = trial_to_stable_ntwk(trial, pre, C_, P)
        
        # compute replay trigger
        trigger = make_replay_trigger(ntwk, vs_0, gs_0, spks_forced, p, C, P)
        
        # save recurrent cxns, pfcs, and replay trigger
        aux.save(cache_file, {
            'ws_rcr': ntwk.ws_rcr, 'pfcs': ntwk.pfcs, 'trigger': trigger})
        
        print('Cache file saved at {}.'.format(cache_file))
    
    np.random.seed(seed)
    
    # load cache file
    cached = aux.load(cache_file)

    ws_rcr = cached['ws_rcr']
    pfcs = cached['pfcs']
    trigger = cached['trigger']
    
    # get PC mask
    pc_mask = np.all(~np.isnan(pfcs), axis=0)
    pfcs_pc = pfcs[:, pc_mask]
    n_pc = pc_mask.sum()

    print('Running full smln...')
    
    ## build ntwk
    ntwk = p_to_ntwk_plastic(p, P, ws_rcr, pfcs)
    
    ## build stim sequence
    t_final = C.T_REPLAY + (C.N_REPLAY * C.ITVL_REPLAY)
    
    ts = np.arange(0, t_final, P.DT)
    spks_up = np.zeros((len(ts), 2*n_pc))
    
    ### build linear traj-driven PL inputs
    t_mask_traj = (C.TRAJ_START_T <= ts) & (ts < C.TRAJ_END_T)
    ts_traj = ts[t_mask_traj]
    
    xys = np.array([
        np.linspace(C.TRAJ_START_X, C.TRAJ_END_X, len(ts_traj)),
        np.linspace(C.TRAJ_START_Y, C.TRAJ_END_Y, len(ts_traj))]).T
    
    #### set place-field widths and max rates
    pfws = P.L_PL * np.ones(pfcs_pc.shape[1])
    pf_max_rates = P.R_MAX_PL * np.ones(pfcs_pc.shape[1])
    
    #### add traj-spks to upstream stim
    spks_up[t_mask_traj, :n_pc] = spks_up_from_traj(
        ts_traj, xys, pfcs_pc, pfws, pf_max_rates)
    
    ### build EC inputs
    t_mask_ec = (ts >= C.T_EC)
    spks_up[t_mask_ec, n_pc:] = np.random.poisson(
        p['FR_EC']*P.DT, (t_mask_ec.sum(), n_pc))
    
    ### build replay-triggering inputs
    for ctr in range(C.N_REPLAY):
        t_trigger = C.T_REPLAY + ctr*C.ITVL_REPLAY
        spks_up[int(t_trigger/P.DT), :n_pc] = trigger
        
    # run ntwk
    rsp = ntwk.run(spks_up, dt=P.DT)
    rsp.p = p
    rsp.pfcs = pfcs
    rsp.cell_types = cell_types
    
    # calculate a couple quick replay metrics
    replay_fr, replay_min_fr, replay_max_fr = get_replay_metrics(rsp, p, C, P)
    
    rsp.replay_fr = replay_fr
    rsp.replay_min_fr = replay_min_fr
    rsp.replay_max_fr = replay_max_fr
        
    # save to db if desired
    if save:
        session = make_session()
        
        full_trial = d_models.LinRidgeFullTrial(
            lin_ridge_trial_id=trial.id,
            commit=commit,
            seed=seed,
            replay_fr=replay_fr,
            replay_min_fr=replay_min_fr,
            replay_max_fr=replay_max_fr)
        
        session.add(full_trial)
        session.commit()
        
        session.close()
        
    return rsp


def make_replay_trigger(ntwk, vs_0, gs_0, spks_forced, p, C, P):
    """
    Construct a set of upstream PL input spks that have approximately
    the same effect as the explicitly forced spks used previously.
    """
    # identify idxs of forced PCs
    pc_mask = np.all(~np.isnan(ntwk.pfcs), axis=0)
    n_pc = pc_mask.sum()
    
    pcs_forced = np.unique(np.nonzero(spks_forced[pc_mask])[1])
    n_forced = len(pcs_forced)
    
    # loop over increasing numbers of upstream PL spks until total ntwk 
    # activation over a few ms is the same or greater than the number
    # of equivalent explicitly forced spks
    trigger = np.zeros(n_pc)
    
    for ctr in np.arange(0, int(C.PL_TRIGGER_FR_MAX/C.PL_TRIGGER_FR_INCMT)):
        
        # increase PL spks by random incmt
        incmt = np.random.poisson(C.PL_TRIGGER_FR_INCMT, n_forced)
        trigger[pcs_forced] += incmt
        
        # build spks_up from PL cells with this trigger
        ts = np.arange(0, C.PL_TRIGGER_RUN_TIME, P.DT)
        
        spks_up = np.zeros((len(ts), 2*n_pc))
        spks_up[1, :n_pc] = trigger
        
        rsp = ntwk.run(spks_up, vs_0=vs_0, gs_0=gs_0, dt=P.DT)
        
        # break if number of spks is greater than number originally forced
        if rsp.spks.sum() > n_forced:
            break
    else:
        print('Max PL input reached without producing equivalent forced spks.')
        
    # make sure trigger yields replay for slightly longer time course
    ts = np.arange(0, C.PL_TRIGGER_TEST_RUN_TIME, P.DT)
    
    spks_up = np.zeros((len(ts), 2*n_pc))
    spks_up[1, :n_pc] = trigger
    
    rsp = ntwk.run(spks_up, vs_0=vs_0, gs_0=gs_0, dt=P.DT)
    
    if rsp.spks.sum() < C.MIN_SPK_CT_FACTOR_TRIGGER_TEST * n_forced:
        print('Identified trigger potentially did not yield replay.')
    
    return trigger


def p_to_ntwk_plastic(p, P, ws_rcr, pfcs):
    """
    Create a plastic ntwk from a set of params.
    
    :param p: dict of ntwk params
    :param ws_rcr: recurrent weight matrices (optional)
    :param pfcs: place fields of PCs (nans for INHs)
    """
    cc = np.concatenate
    
    # ensure consistent cell counts
    n = pfcs.shape[1]
    
    for w_rcr in ws_rcr.values():
        assert w_rcr.shape[0] == w_rcr.shape[1] == n
    
    pc_mask = np.all(~np.isnan(pfcs), axis=0)
    inh_mask = ~pc_mask
    
    n_pc = pc_mask.sum()
    n_inh = inh_mask.sum()
    
    cell_types = np.repeat('', n)
    cell_types[pc_mask] = 'PC'
    cell_types[inh_mask] = 'INH'
    
    # build ntwk
    
    ## single-unit params
    t_m = cc([np.repeat(P.T_M_PC, n_pc), np.repeat(P.T_M_INH, n_inh)])
    e_l = cc([np.repeat(P.E_L_PC, n_pc), np.repeat(P.E_L_INH, n_inh)])
    v_th = cc([np.repeat(P.V_TH_PC, n_pc), np.repeat(P.V_TH_INH, n_inh)])
    v_reset = cc([np.repeat(P.V_RESET_PC, n_pc), np.repeat(P.V_RESET_INH, n_inh)])
    t_r = cc([np.repeat(P.T_R_PC, n_pc), np.repeat(P.T_R_INH, n_inh)])
    
    ## upstream cxns
    
    ### AMPA inputs from PL
    w_up_a = np.zeros((n, 2*n_pc), dtype=float)
    w_up_a[:n_pc, :n_pc] = P.W_A_PC_PL * np.eye(n_pc)
    
    ### NMDA inputs from EC
    w_up_n = np.zeros((n, 2*n_pc), dtype=float)
    w_up_n[:n_pc, n_pc:] = P.W_N_PC_EC_I * np.eye(n_pc)
    
    ### no GABA inputs
    w_up_g = np.zeros((n, 2*n_pc), dtype=float)
    
    ws_up = {'AMPA': w_up_a, 'NMDA': w_up_n, 'GABA': w_up_g}
    
    ## plasticity params
    plasticity={
        'masks': {
            'AMPA': np.zeros((n, 2*n_pc), dtype=bool),
            'NMDA': w_up_n > 0,
            'GABA': np.zeros((n, 2*n_pc), dtype=bool),
        },
        'w_ec_ca3_maxs': {
            'AMPA': np.nan,
            'NMDA': P.W_N_PC_EC_F,
            'GABA': np.nan,
        },
        'T_W': P.T_W, 'T_C': P.T_C, 'C_S': P.C_S, 'BETA_C': P.B_C
    }
    
    # final ntwk
    ntwk = LIFNtwk(
        t_m=t_m, e_l=e_l, v_th=v_th, v_reset=v_reset, t_r=t_r,
        e_ahp=P.E_AHP_PC, t_ahp=np.inf, w_ahp=0,
        es_syn={'AMPA': P.E_A, 'NMDA': P.E_N, 'GABA': P.E_G},
        ts_syn={'AMPA': P.T_A, 'NMDA': P.T_N, 'GABA': P.T_G},
        ws_up=ws_up, ws_rcr=ws_rcr, plasticity=plasticity)
    
    ntwk.pfcs = pfcs
    ntwk.cell_types = cell_types
    
    return ntwk


def get_replay_metrics(rsp, p, C, P):
    """
    Calculate average replay-epoch firing rate (replay_fr), then
    divide area into N horizontal stripes and return average replay-epoch
    frs of stripes with min (replay_fr_min) and max (replay_fr_max) firing
    rates.
    """
    pc_mask = np.all(~np.isnan(rsp.pfcs), axis=0)
    n_pc = pc_mask.sum()
    
    t_mask_replay = (ts >= C.T_REPLAY)
    spks_pc_replay = rsp.spks[:, pc_mask][t_mask_replay]
    
    # calculate mean PC activation in replay epoch
    replay_fr = np.mean(spks_pc_replay.sum(1) / P.DT / n_pc)
    
    # calculate replay firing rate in each stripe
    ys_pc = rsp.pfcs[1, pc_mask]
    
    stripe_height = p['AREA_H'] / C.N_STRIPES
    
    replay_frs_stripe = np.nan * np.zeros(C.N_STRIPES)
    
    for ctr in range(C.N_STRIPES):
        
        # get stripe mask
        y_min = -(p['AREA_H']/2) + (ctr * stripe_height)
        y_max = y_min_stripe + stripe_height
        
        stripe_mask = (y_min <= ys_pc) & (ys_pc < y_max)
        n_pc_stripe = stripe_mask.sum()
        
        replay_fr_stripe = np.mean(
            spks_pc_replay[:, stripe_mask].sum(1) /P.DT / n_pc_stripe)
        
        replay_frs_stripe[ctr] = replay_fr_stripe
        
    return replay_fr, replay_fr_min, replay_fr_max

