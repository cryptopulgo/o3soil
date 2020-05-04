import openseespy.opensees as opy
import o3seespy as o3
import o3soil.sra
import copy
import sfsimodels as sm
import json
import numpy as np
import eqsig
from tests.conftest import TEST_DATA_DIR

# for linear analysis comparison
import liquepy as lq
import pysra
from bwplot import cbox
import json


def run():
    sl = sm.Soil()
    sl.type = 'pimy'
    vs = 160.
    unit_mass = 1700.0
    sl.cohesion = 58.0e3
    sl.phi = 0.0
    sl.g_mod = vs ** 2 * unit_mass
    sl.poissons_ratio = 0.0
    sl.phi = 0.0
    sl.unit_dry_weight = unit_mass * 9.8
    sl.specific_gravity = 2.65
    sl.peak_strain = 0.01  # set additional parameter required for PIMY model
    ref_press = 100.e3
    sl.xi = 0.03  # for linear analysis
    sl.sra_type = 'hyperbolic'
    o3soil.backbone.set_params_from_op_pimy_model(sl, ref_press)
    sl.inputs += ['strain_curvature', 'xi_min', 'sra_type', 'strain_ref', 'peak_strain']
    assert np.isclose(vs, sl.get_shear_vel(saturated=False))
    soil_profile = sm.SoilProfile()
    soil_profile.add_layer(0, sl)

    sl = sm.Soil()
    sl.type = 'pimy'
    vs = 400.
    unit_mass = 1700.0
    sl.g_mod = vs ** 2 * unit_mass
    sl.poissons_ratio = 0.0
    sl.cohesion = 395.0e3
    sl.phi = 0.0
    sl.unit_dry_weight = unit_mass * 9.8
    sl.specific_gravity = 2.65
    sl.peak_strain = 0.1  # set additional parameter required for PIMY model
    sl.xi = 0.03  # for linear analysis
    sl.sra_type = 'hyperbolic'
    o3soil.backbone.set_params_from_op_pimy_model(sl, ref_press)
    sl.inputs += ['strain_curvature', 'xi_min', 'sra_type', 'strain_ref', 'peak_strain']
    soil_profile.add_layer(9.5, sl)
    soil_profile.height = 20.0
    ecp_out = sm.Output()
    ecp_out.add_to_dict(soil_profile)
    ofile = open('ecp.json', 'w')
    ofile.write(json.dumps(ecp_out.to_dict(), indent=4))
    ofile.close()
    mods = sm.load_json('ecp.json', default_to_base=True)
    soil_profile = mods['soil_profile'][1]


    record_path = TEST_DATA_DIR
    record_filename = 'short_motion_dt0p01.txt'
    in_sig = eqsig.load_asig(TEST_DATA_DIR + record_filename)

    # linear analysis with pysra
    od = lq.sra.run_pysra(soil_profile, in_sig, odepths=np.array([0.0, 2.0]))
    pysra_sig = eqsig.AccSignal(od['ACCX'][0], in_sig.dt)

    outputs = o3soil.sra.site_response(soil_profile, in_sig)
    resp_dt = outputs['time'][2] - outputs['time'][1]
    surf_sig = eqsig.AccSignal(outputs['ACCX'][0], resp_dt)

    o3_surf_vals = np.interp(pysra_sig.time, surf_sig.time, surf_sig.values)

    show = 1

    if show:
        import matplotlib.pyplot as plt
        from bwplot import cbox

        bf, sps = plt.subplots(nrows=3)

        sps[0].plot(in_sig.time, in_sig.values, c='k', label='Input')
        # sps[0].plot(pysra_sig.time, o3_surf_vals, c=cbox(0), label='o3')
        sps[0].plot(outputs['time'], outputs['ACCX'][0], c=cbox(3), label='o3')
        sps[0].plot(pysra_sig.time, pysra_sig.values, c=cbox(1), label='pysra')

        sps[1].plot(in_sig.fa_frequencies, abs(in_sig.fa_spectrum), c='k')
        sps[1].plot(surf_sig.fa_frequencies, abs(surf_sig.fa_spectrum), c=cbox(0))
        sps[1].plot(pysra_sig.fa_frequencies, abs(pysra_sig.fa_spectrum), c=cbox(1))
        sps[1].set_xlim([0, 20])
        h = surf_sig.smooth_fa_spectrum / in_sig.smooth_fa_spectrum
        sps[2].plot(surf_sig.smooth_fa_frequencies, h, c=cbox(0))
        pysra_h = pysra_sig.smooth_fa_spectrum / in_sig.smooth_fa_spectrum
        sps[2].plot(pysra_sig.smooth_fa_frequencies, pysra_h, c=cbox(1))
        sps[2].axhline(1, c='k', ls='--')
        sps[0].plot(pysra_sig.time, (o3_surf_vals - pysra_sig.values) * 10, c='r', label='Error x10', lw=0.5)
        sps[0].legend()
        plt.show()

    assert np.isclose(o3_surf_vals, pysra_sig.values, atol=0.01, rtol=100).all()


if __name__ == '__main__':
    run()

