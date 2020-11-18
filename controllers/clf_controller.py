from controllers.inverse_dynamics_controller import *

class CLFController(IDController):
    """
    A Control Lyapunov Function (CLF) QP-based inverse dynamics controller,
    following http://ames.caltech.edu/reher2019inverse.pdf.

    Takes as input desired positions/velocities/accelerations of the 
    feet and floating base and computes corresponding joint torques. 
    """
    def __init__(self, plant, dt, use_lcm=False):
        IDController.__init__(self, plant, dt, use_lcm=use_lcm)

        # Solve CARE to determine candidate CLF eta'*P*eta
        # TODO: consider changing size of eta
        self.F = np.block([[np.zeros((6,6)), np.eye(6)      ],    # task-space dynamics
                           [np.zeros((6,6)), np.zeros((6,6))]])   # eta_dot = F*eta + G*nu
        self.G = np.block([[np.zeros((6,6))],                     # eta = [x_tilde, xd_tilde]
                           [np.eye(6)]])                          # nu = xdd_tilde
        
        Q = 100*np.eye(12)
        R = np.eye(6)

        self.P = ContinuousAlgebraicRiccatiEquation(self.F,self.G,Q,R)

        self.gamma = np.min(np.linalg.eigvals(Q)) / np.max(np.linalg.eigvals(self.P))

    def AddVdotCost(self, x_tilde, xd_tilde, J, vd, weight=1):
        """
        Add a cost penalizing the time derivative of the Lyapunov function

            V = eta'*P*eta

        where eta = [x_tilde;xd_tilde]
        """
        eta = np.hstack([x_tilde,xd_tilde])
        a = weight*2*eta.T@self.P@self.G@J
        return self.mp.AddLinearCost(a,b=0.0,vars=vd)

    def AddVdotConstraint(self, x_tilde, xd_tilde, J, vd, Jdv, xdd_nom, delta):
        """
        Add a constraint Vdot <= -gamma*V + delta to the whole-body QP, where
            
            V = eta'*P*eta

        and eta = [x_tilde;xd_tilde]
        """
        eta = np.hstack([x_tilde, xd_tilde])
        V = eta.T@self.P@eta

        # We'll write as lb <= A*x <= ub
        lb = np.asarray(-np.inf).reshape(1,)
        A = np.hstack([2*eta.T@self.P@self.G@J, -1])[np.newaxis]
        x = np.vstack([vd,delta])
        ub = -self.gamma*V - 2*eta.T@self.P@self.F@eta -2*eta.T@self.P@self.G@(Jdv - xdd_nom)
        ub = np.asarray(ub).reshape(1,)

        return self.mp.AddLinearConstraint(A=A,lb=lb,ub=ub,vars=x)


    def ControlLaw(self, context, q, v):
        """
        A CLF-QP whole-body controller

           minimize:
               || J*vd + Jd*v - xd_nom ||^2 + Vdot
           subject to:
                Vdot <= -gamma*V +delta
                M*vd + Cv + tau_g = S'*tau + sum(J'*f)
                f \in friction cones
                J_cj*vd + Jd_cj*v == 0

        where

            V = [x_tilde ]^T * P * [x_tilde ]
                [xd_tilde]         [xd_tilde]
        """
        ######### Tuning Parameters #########
        Kp_body_p = 100.0
        Kd_body_p = 10.0

        Kp_body_rpy = Kp_body_p
        Kd_body_rpy = Kd_body_p

        Kp_foot = 200.0
        Kd_foot = 20.0

        w_body = 10.0
        w_foot = 1.0
        #####################################
       
        # Compute Dynamics Quantities
        M, Cv, tau_g, S = self.CalcDynamics()

        # Get setpoint data from the trunk model
        trunk_data = self.EvalAbstractInput(context,1).get_value()
        
        contact_feet = trunk_data["contact_states"]       # Note: it may be better to determine
        swing_feet = [not foot for foot in contact_feet]  # contact states from the actual robot rather than
        num_contact = sum(contact_feet)                   # the planned trunk trajectory.
        num_swing = sum(swing_feet)

        p_body_nom = trunk_data["p_body"]
        pd_body_nom = trunk_data["pd_body"]
        pdd_body_nom = trunk_data["pdd_body"]

        rpy_body_nom = trunk_data["rpy_body"]
        rpyd_body_nom = trunk_data["rpyd_body"]
        rpydd_body_nom = trunk_data["rpydd_body"]
        
        p_feet_nom = np.array([trunk_data["p_lf"],trunk_data["p_rf"],trunk_data["p_lh"],trunk_data["p_rh"]])
        pd_feet_nom = np.array([trunk_data["pd_lf"],trunk_data["pd_rf"],trunk_data["pd_lh"],trunk_data["pd_rh"]])
        pdd_feet_nom = np.array([trunk_data["pdd_lf"],trunk_data["pdd_rf"],trunk_data["pdd_lh"],trunk_data["pdd_rh"]])

        p_s_nom = p_feet_nom[swing_feet]
        pd_s_nom = pd_feet_nom[swing_feet]
        pdd_s_nom = pdd_feet_nom[swing_feet]

        # Get robot's actual task-space (body pose + foot positions) data
        X_body, J_body, Jdv_body = self.CalcFramePoseQuantities(self.body_frame)
        
        p_body = X_body.translation()
        pd_body = (J_body@v)[3:]

        RPY_body = RollPitchYaw(X_body.rotation())  # RPY object helps convert between angular velocity and rpyd
        rpy_body = RPY_body.vector()
        omega_body = (J_body@v)[:3]   # angular velocity of the body
        rpyd_body = RPY_body.CalcRpyDtFromAngularVelocityInParent(omega_body)

        p_lf, J_lf, Jdv_lf = self.CalcFramePositionQuantities(self.lf_foot_frame)
        p_rf, J_rf, Jdv_rf = self.CalcFramePositionQuantities(self.rf_foot_frame)
        p_lh, J_lh, Jdv_lh = self.CalcFramePositionQuantities(self.lh_foot_frame)
        p_rh, J_rh, Jdv_rh = self.CalcFramePositionQuantities(self.rh_foot_frame)
       
        p_feet = np.array([p_lf, p_rf, p_lh, p_rh]).reshape(4,3)
        J_feet = np.array([J_lf, J_rf, J_lh, J_rh])
        Jdv_feet = np.array([Jdv_lf, Jdv_rf, Jdv_lh, Jdv_rh])
        pd_feet = J_feet@v

        p_s = p_feet[swing_feet]
        pd_s = pd_feet[swing_feet]

        J_c = J_feet[contact_feet]
        Jdv_c = Jdv_feet[contact_feet]
        
        J_s = J_feet[swing_feet]
        Jdv_s = Jdv_feet[swing_feet]

        # Additional task-space dynamics terms
        if any(swing_feet):
            J = np.vstack([J_body, np.vstack(J_s)])
            Jdv = np.hstack([Jdv_body, np.vstack(Jdv_s).flatten()])
        else:
            J = J_body
            Jdv = Jdv_body

        # Task-space states and errors
        x = np.hstack([rpy_body, p_body, p_s.flatten()])
        xd = np.hstack([RPY_body.CalcAngularVelocityInParentFromRpyDt(rpyd_body),
                        pd_body,
                        pd_s.flatten()])

        x_nom = np.hstack([rpy_body_nom, p_body_nom, p_s_nom.flatten()])
        xd_nom = np.hstack([RPY_body.CalcAngularVelocityInParentFromRpyDt(rpyd_body_nom),
                            pd_body_nom,
                            pd_s_nom.flatten()])
        xdd_nom = np.hstack([RPY_body.CalcAngularVelocityInParentFromRpyDt(rpydd_body_nom),
                             pdd_body_nom, 
                             pdd_s_nom.flatten()])

        x_tilde = x - x_nom
        xd_tilde = xd - xd_nom

        # Feedback gain and weighting matrices
        nf = 3*sum(swing_feet)   # there are 3 foot-related variables (x,y,z positions) for each swing foot
       
        Kp = np.block([[ np.kron(np.diag([Kp_body_rpy, Kp_body_p]),np.eye(3)), np.zeros((6,nf))   ],
                       [ np.zeros((nf,6)),                                     Kp_foot*np.eye(nf) ]])
        
        Kd = np.block([[ np.kron(np.diag([Kd_body_rpy, Kd_body_p]),np.eye(3)), np.zeros((6,nf))   ],
                       [ np.zeros((nf,6)),                                     Kd_foot*np.eye(nf) ]])


        # Set up and solve the QP
        self.mp = MathematicalProgram()
        
        vd = self.mp.NewContinuousVariables(self.plant.num_velocities(), 1, 'vd')
        tau = self.mp.NewContinuousVariables(self.plant.num_actuators(), 1, 'tau')
        f_c = [self.mp.NewContinuousVariables(3,1,'f_%s'%j) for j in range(num_contact)]
        delta = self.mp.NewContinuousVariables(1,'delta')

        # min || J*vd + Jd*v - xdd_nom ||^2
        self.AddJacobianTypeCost(J, vd, Jdv, xdd_nom, weight=1.0)

        # min Vdot
        self.AddVdotCost(x_tilde, xd_tilde, J, vd, weight=1)

        # s.t. Vdot <= -gamma*V + delta
        self.AddVdotConstraint(x_tilde, xd_tilde, J, vd, Jdv, xdd_nom, delta)

        # s.t. delta <= 0
        self.mp.AddLinearConstraint( delta[0] <= 0 )

        # s.t.  M*vd + Cv + tau_g = S'*tau + sum(J_c[j]'*f_c[j])
        self.AddDynamicsConstraint(M, vd, Cv, tau_g, S, tau, J_c, f_c)

        if any(contact_feet):
            # s.t. f_c[j] in friction cones
            self.AddFrictionPyramidConstraint(f_c)

            # s.t. J_cj*vd + Jd_cj*v == 0 (+ some daming)
            self.AddContactConstraint(J_c, vd, Jdv_c, v)
    
        result = self.solver.Solve(self.mp)
        assert result.is_success()
        tau = result.GetSolution(tau)
        vd = result.GetSolution(vd)

        # Logging
        eta = np.hstack([x_tilde,xd_tilde])
        self.V = eta.T@self.P@eta
        self.err = x_tilde.T@x_tilde
        self.Vdot = 2*eta.T@self.P@self.F@eta + 2*eta.T@self.P@self.G@(J@vd + Jdv - xdd_nom)

        return tau
