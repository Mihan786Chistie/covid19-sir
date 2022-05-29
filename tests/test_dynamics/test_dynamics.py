#!/usr/bin/env python
# -*- coding: utf-8 -*-

import numpy as np
import pytest
from covsirphy import Dynamics, SIRFModel, Term, Validator, EmptyError


@pytest.mark.parametrize("model", [SIRFModel])
class TestDynamics(object):
    def test_one_phase(self, model):
        dyn = Dynamics.from_sample(model)
        del dyn.name
        dyn.name = "Dynamics"
        assert dyn.name == "Dynamics"
        registered_df = dyn.register()
        Validator(registered_df).dataframe(columns=[Term.DATE, *SIRFModel._VARIABLES, *SIRFModel._PARAMETERS])
        summary_df = dyn.summary()
        assert len(summary_df) == 1
        Validator(dyn.simulate()).dataframe(columns=[Term.DATE, Term.S, Term.CI, Term.F, Term.R])
        Validator(dyn.simulate(model_specific=True)).dataframe(columns=[Term.DATE, *SIRFModel._VARIABLES])

    def test_two_phase(self, model):
        dyn = Dynamics.from_sample(model)
        registered_df = dyn.register()
        with pytest.raises(EmptyError):
            failed_df = registered_df.copy()
            failed_df[Term.CI] = np.nan
            dyn.register(failed_df)
        registered_df.loc[registered_df.index[90], "rho"] = registered_df.loc[registered_df.index[0], "rho"] * 2
        dyn.register(data=registered_df)
        summary_df = dyn.summary()
        assert len(summary_df) == 2
        Validator(dyn.simulate()).dataframe(columns=[Term.DATE, Term.S, Term.CI, Term.F, Term.R])
