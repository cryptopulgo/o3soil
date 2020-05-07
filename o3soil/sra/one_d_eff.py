import numpy as np
import sfsimodels as sm
import openseespy.opensees as opy
import o3seespy as o3
import o3seespy.extensions
import copy
import os
from o3soil.sra.output import O3SRAOutputs


class SRA1D(object):
    osi = None

    def __init__(self, sp, dy=0.5, k0=0.5, base_imp=0, cache_path=None, opfile=None):
        self.sp = sp
        sp.gen_split(props=['shear_vel', 'unit_mass'], target=dy)
        thicknesses = sp.split["thickness"]
        self.n_node_rows = len(thicknesses) + 1
        node_depths = -np.cumsum(sp.split["thickness"])
        self.node_depths = np.insert(node_depths, 0, 0)
        self.ele_depths = (self.node_depths[1:] + self.node_depths[:-1]) / 2
        self.unit_masses = sp.split["unit_mass"] / 1e3

        self.grav = 9.81

        self.k0 = k0
        self.base_imp = base_imp

        self.ele_width = 3 * min(thicknesses)
        self.cache_path = cache_path
        self.opfile = opfile
        # Defined in static analysis
        self.soil_mats = None
        self.eles = None
        self.sn = None  # soil nodes

    def build_model(self):
        # Define nodes and set boundary conditions for simple shear deformation
        # Start at top and build down?
        if self.opfile:
            self.state = 3
        else:
            self.state = 0
        if self.osi is None:
            self.osi = o3.OpenSeesInstance(ndm=2, ndf=3, state=self.state)
        nx = 1
        sn = []
        # sn = [[o3.node.Node(osi, ele_width * j, 0) for j in range(nx + 1)]]
        for i in range(0, self.n_node_rows):
            # Establish left and right nodes
            sn.append([o3.node.Node(self.osi, self.ele_width * j, self.node_depths[i]) for j in range(nx + 1)])
            # set x and y dofs equal for left and right nodes
            if i != self.n_node_rows - 1:
                o3.EqualDOF(self.osi, sn[i][0], sn[i][-1], [o3.cc.DOF2D_X, o3.cc.DOF2D_Y, o3.cc.DOF2D_PP])
        sn = np.array(sn)

        if self.base_imp < 0:
            # Fix base nodes
            for j in range(nx + 1):
                o3.Fix3DOF(self.osi, sn[-1][j], o3.cc.FIXED, o3.cc.FIXED, o3.cc.FIXED)  # Fixed pore pressure at base
        else:
            # Fix base nodes
            for j in range(nx + 1):
                o3.Fix3DOF(self.osi, sn[-1][j], o3.cc.FREE, o3.cc.FIXED, o3.cc.FIXED)  # Fixed pore pressure at base

            # Define dashpot nodes
            self.osi.reset_model_params(2, ndf=2)
            dashpot_node_l = o3.node.Node(self.osi, 0, self.node_depths[-1])
            dashpot_node_2 = o3.node.Node(self.osi, 0, self.node_depths[-1])
            o3.Fix3DOF(self.osi, dashpot_node_l, o3.cc.FIXED, o3.cc.FIXED, o3.cc.FREE)
            o3.Fix3DOF(self.osi, dashpot_node_2, o3.cc.FREE, o3.cc.FIXED, o3.cc.FREE)

            # define equal DOF for dashpot and soil base nodes
            o3.EqualDOF(self.osi, sn[-1][0], sn[-1][1], [o3.cc.X])
            o3.EqualDOF(self.osi, sn[-1][0], dashpot_node_2, [o3.cc.X])

        # define materials
        pois = self.k0 / (1 + self.k0)
        ele_thick = 1.0  # m
        self.soil_mats = []
        strains = np.logspace(-6, -0.5, 16)
        prev_args = []
        prev_kwargs = {}
        prev_sl_class = None
        self.eles = []
        for i in range(len(self.ele_depths)):
            y_depth = self.ele_depths[i]

            sl_id = self.sp.get_layer_index_by_depth(-y_depth)
            sl = self.sp.layer(sl_id)

            if hasattr(sl, 'op_type'):
                if sl.built:
                    pass
                else:
                    sl.build(self.osi)
                    mat = sl
                    self.soil_mats.append(mat)
            else:
                app2mod = {}
                if y_depth > self.sp.gwl:
                    umass = sl.unit_sat_mass / 1e3  # TODO: work out how to run in Pa, N, m, s
                else:
                    umass = sl.unit_dry_mass / 1e3
                overrides = {'nu': pois, 'p_atm': 101,
                             'rho': umass,
                             'unit_moist_mass': umass,
                             'nd': 2.0,
                             # 'n_surf': 25
                             }
                # Define material
                if not hasattr(sl, 'o3_type'):
                    sl.o3_type = sl.type  # for backward compatibility
                if sl.o3_type == 'pm4sand':
                    sl_class = o3.nd_material.PM4Sand
                    # overrides = {'nu': pois, 'p_atm': 101, 'unit_moist_mass': umass}
                    app2mod = sl.app2mod
                elif sl.o3_type == 'sdmodel':
                    sl_class = o3.nd_material.StressDensity
                    # overrides = {'nu': pois, 'p_atm': 101, 'unit_moist_mass': umass}
                    app2mod = sl.app2mod
                elif sl.o3_type in ['pimy', 'pdmy', 'pdmy02']:
                    if hasattr(sl, 'get_g_mod_at_m_eff_stress'):
                        if hasattr(sl, 'g_mod_p0') and sl.g_mod_p0 != 0.0:
                            v_eff = self.sp.get_v_eff_stress_at_depth(y_depth)
                            k0 = sl.poissons_ratio / (1 - sl.poissons_ratio)
                            m_eff = v_eff * (1 + 2 * k0) / 3
                            p = m_eff  # Pa
                            overrides['d'] = 0.0
                        else:
                            p = 101.0e3  # Pa
                            overrides['d'] = sl.a
                        g_mod_r = sl.get_g_mod_at_m_eff_stress(p) / 1e3
                    else:
                        p = 101.0e3  # Pa
                        overrides['d'] = 0.0
                        g_mod_r = sl.g_mod / 1e3

                    b_mod = 2 * g_mod_r * (1 + sl.poissons_ratio) / (3 * (1 - 2 * sl.poissons_ratio))
                    overrides['p_ref'] = p / 1e3
                    overrides['g_mod_ref'] = g_mod_r
                    overrides['bulk_mod_ref'] = b_mod
                    if sl.type == 'pimy':
                        overrides['cohesion'] = sl.cohesion / 1e3
                        sl_class = o3.nd_material.PressureIndependMultiYield
                    elif sl.type == 'pdmy':
                        sl_class = o3.nd_material.PressureDependMultiYield
                    elif sl.type == 'pdmy02':
                        sl_class = o3.nd_material.PressureDependMultiYield02
                else:
                    sl_class = o3.nd_material.ElasticIsotropic
                    sl.e_mod = 2 * sl.g_mod * (1 - sl.poissons_ratio) / 1e3
                    overrides['nu'] = sl.poissons_ratio
                    app2mod['rho'] = 'unit_moist_mass'
                args, kwargs = o3.extensions.get_o3_kwargs_from_obj(sl, sl_class, custom=app2mod, overrides=overrides)

                if o3.extensions.has_o3_model_changed(sl_class, prev_sl_class, args, prev_args, kwargs, prev_kwargs):
                    mat = sl_class(self.osi, *args, **kwargs)
                    prev_sl_class = sl_class
                    prev_args = copy.deepcopy(args)
                    prev_kwargs = copy.deepcopy(kwargs)
                    mat.dynamic_poissons_ratio = sl.poissons_ratio
                    self.soil_mats.append(mat)

            # def element
            for xx in range(nx):
                nodes = [sn[i + 1][xx], sn[i + 1][xx + 1], sn[i][xx + 1], sn[i][xx]]  # anti-clockwise
                # eles.append(o3.element.Quad(self.osi, nodes, ele_thick, o3.cc.PLANE_STRAIN, mat, b2=-grav * unit_masses[i]))
                # osi, ele_nodes, mat, thick, f_bulk, f_den, k1, k2, void, alpha, b1=0.0, b2=0.0
                k_water = 2.2e6
                a_sspquad_up = 6.0e-5
                self.eles.append(o3.element.SSPquadUP(self.osi, nodes, mat, ele_thick, k_water, f_den=1.0, k1=sl.permeability,
                                                      k2=sl.permeability, void=sl.e_curr, alpha=a_sspquad_up,
                                                      b2=-self.grav))
        self.sn = sn
        if self.base_imp >= 0:
            # define material and element for viscous dampers
            if self.base_imp == 0:
                sl = self.sp.get_soil_at_depth(self.sp.height)
                base_imp = sl.unit_dry_mass * self.sp.get_shear_vel_at_depth(self.sp.height)
            c_base = self.ele_width * base_imp / 1e3
            dashpot_mat = o3.uniaxial_material.Viscous(self.osi, c_base, alpha=1.)
            o3.element.ZeroLength(self.osi, [dashpot_node_l, dashpot_node_2], mats=[dashpot_mat], dirs=[o3.cc.DOF2D_X])

        self.o3res = o3.results.Results2D(cache_path=self.cache_path)
        self.o3res.wipe_old_files()
        self.o3res.coords = o3.get_all_node_coords(self.osi)
        self.o3res.ele2node_tags = o3.get_all_ele_node_tags_as_dict(self.osi)
        self.o3res.mat2ele_tags = []
        for ele in self.eles:
            self.o3res.mat2ele_tags.append([ele.mat.tag, ele.tag])

    def execute_static(self):
        # Static analysis
        o3.constraints.Transformation(self.osi)
        o3.test.NormDispIncr(self.osi, tol=1.0e-5, max_iter=30, p_flag=0)
        o3.algorithm.Newton(self.osi)
        o3.numberer.RCM(self.osi)
        o3.system.ProfileSPD(self.osi)
        o3.integrator.Newmark(self.osi, gamma=0.5, beta=0.25)
        o3.analysis.Transient(self.osi)
        o3.analyze(self.osi, 1000, 5.)
        if self.opfile:
            o3.extensions.to_py_file(self.osi, self.opfile)
            o3.extensions.to_tcl_file(self.osi, self.opfile.replace('.py', '.tcl'))

        # for i in range(len(self.soil_mats)):
        #     if hasattr(self.soil_mats[i], 'update_to_nonlinear'):
        #         self.soil_mats[i].update_to_nonlinear(self.osi)
        for ele in self.eles:
            mat = ele.mat
            if hasattr(mat, 'set_nu'):
                mat.set_nu(mat.dynamic_poissons_ratio, eles=[ele])
                # TODO: set_dynamic permeability
        o3.analyze(self.osi, 40, 500.)

        # reset time and analysis
        o3.wipe_analysis(self.osi)
        self.o3res.coords = o3.get_all_node_coords(self.osi)
        # if self.opfile:
        #     o3.extensions.to_py_file(self.osi, self.opfile)

    def get_nearest_node_layer_at_depth(self, depth):
        # Convert to positive since node depths go downwards
        return int(np.round(np.interp(-depth, -self.node_depths, np.arange(len(self.node_depths)))))

    def get_nearest_ele_layer_at_depth(self, depth):
        # Convert to positive since ele depths go downwards
        return int(np.round(np.interp(-depth, -self.ele_depths, np.arange(len(self.ele_depths)))))

    def apply_loads(self, ray_freqs=(0.5, 10), xi=0.03):
        o3.set_time(self.osi, 0.0)

        # Define the dynamic analysis
        o3.constraints.Transformation(self.osi)
        o3.test.NormDispIncr(self.osi, tol=1.0e-4, max_iter=30, p_flag=0)
        # o3.test_check.EnergyIncr(self.osi, tol=1.0e-6, max_iter=30)
        o3.algorithm.Newton(self.osi)
        o3.system.SparseGeneral(self.osi)
        o3.numberer.RCM(self.osi)
        o3.integrator.Newmark(self.osi, gamma=0.5, beta=0.25)
        o3.analysis.Transient(self.osi)
        omega_1 = 2 * np.pi * ray_freqs[0]
        omega_2 = 2 * np.pi * ray_freqs[1]
        a0 = 2 * xi * omega_1 * omega_2 / (omega_1 + omega_2)
        a1 = 2 * xi / (omega_1 + omega_2)
        o3.rayleigh.Rayleigh(self.osi, a0, a1, 0, 0)

        static_time = 500
        print('time: ', o3.get_time(self.osi))
        # Add static stress bias
        time_series = o3.time_series.Path(self.osi, time=[0, static_time / 2, static_time, 1e3], values=[0, 0.5, 1, 1],
                                          use_last=True)
        o3.pattern.Plain(self.osi, time_series)
        net_hload = 0
        for i in range(len(self.sp.hloads)):
            pload = self.sp.hloads[i].p_x
            y = -self.sp.hloads[i].y
            ind = self.get_nearest_node_layer_at_depth(y)
            print(i, y, ind)
            if self.sp.loads_are_stresses:
                pload *= self.ele_width
            o3.Load(self.osi, self.sn[ind][0], [pload, 0])
            net_hload += pload
        if self.base_imp >= 0:
            o3.Load(self.osi, self.sn[-1][0], [-net_hload, 0])

        static_dt = 0.1
        o3.analyze(self.osi, int(static_time / static_dt), static_dt)
        o3.load_constant(self.osi, time=0)

    def execute_dynamic(self, asig, analysis_dt=0.001, ray_freqs=(0.5, 10), xi=0.03, analysis_time=None,
                        outs=None, rec_dt=None, playback_dt=None, playback=True):
        self.rec_dt = rec_dt
        self.playback_dt = playback_dt
        if rec_dt is None:
            self.rec_dt = asig.dt
        if playback_dt is None:
            self.playback_dt = asig.dt
        o3.set_time(self.osi, 0.0)

        # Define the dynamic analysis
        o3.constraints.Transformation(self.osi)
        o3.test.NormDispIncr(self.osi, tol=1.0e-4, max_iter=30, p_flag=0)
        # o3.test_check.EnergyIncr(self.osi, tol=1.0e-6, max_iter=30)
        o3.algorithm.Newton(self.osi)
        o3.system.SparseGeneral(self.osi)
        o3.numberer.RCM(self.osi)
        o3.integrator.Newmark(self.osi, gamma=0.5, beta=0.25)
        o3.analysis.Transient(self.osi)
        # Rayleigh damping parameters
        omega_1 = 2 * np.pi * ray_freqs[0]
        omega_2 = 2 * np.pi * ray_freqs[1]
        a0 = 2 * xi * omega_1 * omega_2 / (omega_1 + omega_2)
        a1 = 2 * xi / (omega_1 + omega_2)
        o3.rayleigh.Rayleigh(self.osi, a0, a1, 0, 0)

        init_time = o3.get_time(self.osi)
        if playback:
            self.o3res.dynamic = True
            self.o3res.start_recorders(self.osi, dt=self.playback_dt)
        else:
            self.o3res.dynamic = False
        self.o3sra_outs = O3SRAOutputs()
        self.o3sra_outs.start_recorders(self.osi, outs, self.sn, self.eles, rec_dt=self.rec_dt)

        # Define the dynamic input motion
        if self.base_imp < 0:  # fixed base
            acc_series = o3.time_series.Path(self.osi, dt=asig.dt, values=asig.values)
            o3.pattern.UniformExcitation(self.osi, dir=o3.cc.X, accel_series=acc_series)
        else:
            ts_obj = o3.time_series.Path(self.osi, dt=asig.dt, values=asig.velocity * 1, factor=self.c_base)
            o3.pattern.Plain(self.osi, ts_obj)
            o3.Load(self.osi, self.sn[-1][0], [1., 0.])
        if self.state == 3:
            o3.extensions.to_py_file(self.osi, self.opfile)
        # Run the dynamic motion
        o3.record(self.osi)
        while o3.get_time(self.osi) - init_time < analysis_time:
            if o3.analyze(self.osi, 1, analysis_dt):
                print('failed')
                if o3.analyze(self.osi, 10, analysis_dt / 10):
                    break
        o3.wipe(self.osi)
        self.out_dict = self.o3sra_outs.results_to_dict()

        if self.cache_path:
            import o3_plot
            self.o3sra_outs.cache_path = self.cache_path
            self.o3sra_outs.results_to_files()
            self.o3res.save_to_cache()


def run_sra(sp, asig, ray_freqs=(0.5, 10), xi=0.03, analysis_dt=0.001, dy=0.5, analysis_time=None, outs=None,
                  base_imp=0, k0=0.5, cache_path=None, opfile=None, playback=False):
    sra_1d = SRA1D(sp, dy=dy, k0=k0, base_imp=base_imp, cache_path=cache_path, opfile=opfile)
    sra_1d.build_model()
    sra_1d.execute_static()
    if hasattr(sra_1d.sp, 'hloads'):
        sra_1d.apply_loads()
    sra_1d.execute_dynamic(asig, analysis_dt=analysis_dt, ray_freqs=ray_freqs, xi=xi, analysis_time=analysis_time,
                           outs=outs, playback=playback, playback_dt=0.01)
    return sra_1d

