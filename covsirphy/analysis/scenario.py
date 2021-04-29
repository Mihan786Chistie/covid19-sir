#!/usr/bin/env python
# -*- coding: utf-8 -*-

import copy
from datetime import timedelta
import warnings
import sys
import numpy as np
import pandas as pd
from covsirphy.util.argument import find_args
from covsirphy.util.error import deprecate, ScenarioNotFoundError, UnExecutedError
from covsirphy.util.error import NotRegisteredMainError, NotRegisteredExtraError
from covsirphy.util.error import NotInteractiveError
from covsirphy.util.evaluator import Evaluator
from covsirphy.util.term import Term
from covsirphy.visualization.line_plot import line_plot
from covsirphy.visualization.compare_plot import compare_plot
from covsirphy.cleaning.jhu_data import JHUData
from covsirphy.ode.mbase import ModelBase
from covsirphy.regression.reg_handler import RegressionHandler
from covsirphy.analysis.data_handler import DataHandler
from covsirphy.analysis.phase_tracker import PhaseTracker


class Scenario(Term):
    """
    Scenario analysis.

    Args:
        jhu_data (covsirphy.JHUData or None): object of records
        population_data (covsirphy.PopulationData or None): PopulationData object
        country (str): country name (must not be None)
        province (str or None): province name
        tau (int or None): tau value
        auto_complement (bool): if True and necessary, the number of cases will be complemented

    Note:
        @jhu_data and @population_data must be registered with Scenario.register() if not specified here.
    """

    def __init__(self, jhu_data=None, population_data=None,
                 country=None, province=None, tau=None, auto_complement=True):
        # Area name
        if country is None:
            raise ValueError("@country must be specified.")
        self._area = JHUData.area_name(country, province)
        # Initialize data handler
        self._data = DataHandler(country=str(country), province=str(province or self.UNKNOWN))
        self._data.switch_complement(whether=auto_complement)
        # ODE model
        self._model = None
        # Tau value
        self._tau = self._ensure_tau(tau, accept_none=True)
        # Register datasets
        self._tracker_dict = {}
        self.register(jhu_data=jhu_data, population_data=population_data, extras=None)
        # Initialize parameter tracker
        try:
            self._init_trackers()
        except NotRegisteredMainError:
            pass
        # Interactive (True) / script (False) mode
        self._interactive = hasattr(sys, "ps1")
        # Prediction of parameter values in the future phases: {name: RegressorBase)}
        self._reghandler_dict = {}

    def __getitem__(self, key):
        """
        Return the phase series object for the scenario.

        Args:
            key (str): scenario name

        Raises:
            ScenarioNotFoundError: the scenario is not registered

        Returns:
            covsirphy.PhaseTracker
        """
        if key in self._tracker_dict:
            return self._tracker_dict[key]
        raise ScenarioNotFoundError(key)

    def __setitem__(self, key, value):
        """
        Register a phase tracker.

        Args:
            key (str): scenario name
            value (covsirphy.PhaseTracker): phase tracker
        """
        self._ensure_instance(value, PhaseTracker, name="value")
        self._tracker_dict[key] = value

    @property
    def first_date(self):
        """
        str: the first date of the records
        """
        return self._data.first_date

    @first_date.setter
    def first_date(self, date):
        self._data.timepoints(
            first_date=date, last_date=self._data.last_date, today=self._data.today)

    @property
    def last_date(self):
        """
        str: the last date of the records
        """
        return self._data.last_date

    @last_date.setter
    def last_date(self, date):
        try:
            self._ensure_date_order(self._data.today, date)
        except ValueError:
            today = date
        else:
            today = self._data.today
        self.timepoints(
            first_date=self._data.first_date, last_date=date, today=today)

    @property
    def today(self):
        """
        str: reference date to determine whether a phase is a past phase or a future phase
        """
        return self._data.today

    @today.setter
    def today(self, date):
        self.timepoints(
            first_date=self._data.first_date, last_date=self._data.last_date, today=date)

    @property
    def interactive(self):
        """
        bool: interactive mode (display figures) or not

        Note:
            When running scripts, interactive mode cannot be selected.
        """
        return self._interactive

    @interactive.setter
    def interactive(self, is_interactive):
        if not hasattr(sys, "ps1") and is_interactive:
            raise NotInteractiveError
        self._interactive = hasattr(sys, "ps1") and bool(is_interactive)

    def register(self, jhu_data=None, population_data=None, extras=None):
        """
        Register datasets.

        Args:
            jhu_data (covsirphy.JHUData or None): object of records
            population_data (covsirphy.PopulationData or None): PopulationData object
            extras (list[covsirphy.CleaningBase] or None): extra datasets

        Raises:
            TypeError: non-data cleaning instance was included
            UnExpectedValueError: instance of un-expected data cleaning class was included as an extra dataset
        """
        self._data.register(jhu_data=jhu_data, population_data=population_data, extras=extras)
        if self._data.main_satisfied and not self._tracker_dict:
            self.timepoints()

    def timepoints(self, first_date=None, last_date=None, today=None):
        """
        Set the range of data and reference date to determine past/future of phases.

        Args:
            first_date (str or None): the first date of the records or None (min date of main dataset)
            last_date (str or None): the first date of the records or None (max date of main dataset)
            today (str or None): reference date to determine whether a phase is a past phase or a future phase

        Raises:
            NotRegisteredMainError: either JHUData or PopulationData was not registered
            SubsetNotFoundError: failed in subsetting because of lack of data

        Note:
            When @today is None, the reference date will be the same as @last_date (or max date).
        """
        self._data.timepoints(first_date=first_date, last_date=last_date, today=today)
        self._init_trackers()

    def line_plot(self, df, show_figure=True, filename=None, **kwargs):
        """
        Display or save a line plot of the dataframe.

        Args:
            show_figure (bool): whether show figure when interactive mode or not
            filename (str or None): filename of the figure or None (not save) when script mode

        Note:
            When interactive mode and @show_figure is True, display the figure.
            When script mode and filename is not None, save the figure.
            When using interactive shell, we can change the modes by Scenario.interactive = True/False.
        """
        if self._interactive and show_figure:
            return line_plot(df=df, filename=None, **kwargs)
        if not self._interactive and filename is not None:
            return line_plot(df=df, filename=filename, **kwargs)

    def complement(self, **kwargs):
        """
        Complement the number of recovered cases, if necessary.

        Args:
            kwargs: the other arguments of JHUData.subset_complement()

        Returns:
            covsirphy.Scenario: self
        """
        self._data.switch_complement(whether=True, **kwargs)
        return self

    def complement_reverse(self):
        """
        Restore the raw records. Reverse method of covsirphy.Scenario.complement().

        Returns:
            covsirphy.Scenario: self
        """
        self._data.switch_complement(whether=False)
        return self

    def show_complement(self, **kwargs):
        """
        Show the details of complement that was (or will be) performed for the records.

        Args:
            kwargs: keyword arguments of JHUDataComplementHandler() i.e. control factors of complement

        Returns:
            pandas.DataFrame: as the same as JHUData.show_complement()
        """
        self._data.switch_complement(whether=None, **kwargs)
        return self._data.show_complement()

    def _convert_variables(self, abbr, candidates):
        """
        Convert abbreviated variable names to complete names.

        Args:
            abbr (list[str] or str or None): variable names or abbreviated names
            candidates (list[str]): all candidates

        Returns:
            list[str]: complete names of variables

        Note:
            Selectable values of @abbr are as follows.
            - None: return default list, ["Infected", "Recovered", "Fatal"] (changed in the future)
            - list[str]: return the selected variables
            - "all": the all available variables
            - str: abbr, like "CIFR" (Confirmed/Infected/Fatal/Recovered), "CFR", "RC"
        """
        if abbr is None:
            return [self.CI, self.F, self.R]
        if abbr == "all":
            return self._ensure_list(candidates, name="candidates")
        abbr_dict = {"C": self.C, "I": self.CI, "F": self.F, "R": self.R, }
        variables = list(abbr) if isinstance(abbr, str) else abbr
        variables = [abbr_dict.get(v, v) for v in variables]
        return self._ensure_list(variables, candidates=candidates, name="variables")

    def records(self, variables=None, **kwargs):
        """
        Return the records as a dataframe.

        Args:
            variables (list[str] or str or None): variable names or abbreviated names
            kwargs: the other keyword arguments of Scenario.line_plot()

        Raises:
            NotRegisteredMainError: either JHUData or PopulationData was not registered
            SubsetNotFoundError: failed in subsetting because of lack of data
            NotRegisteredExtraError: some variables are not included in the main datasets
            and no extra datasets were registered

        Returns:
            pandas.DataFrame

                Index
                    reset index
                Columns
                    - Date (pd.Timestamp): Observation date
                    - Columns set by @variables (int)

        Note:
            - Records with Recovered > 0 will be selected.
            - If complement was performed by Scenario.complement() or Scenario(auto_complement=True),
            The kind of complement will be added to the title of the figure.

        Note:
            Selectable values of @variables are as follows.
            - None: return default list, ["Infected", "Recovered", "Fatal"] (changed in the future)
            - list[str]: return the selected variables
            - "all": the all available variables
            - str: abbr, like "CIFR" (Confirmed/Infected/Fatal/Recovered), "CFR", "RC"
        """
        # Get necessary data for the variables
        all_df = self._data.records_all().set_index(self.DATE)
        variables = self._convert_variables(variables, all_df.columns.tolist())
        df = all_df.loc[:, variables]
        # Figure
        if self._data.complemented:
            title = f"{self._area}: Cases over time\nwith {self._data.complemented}"
        else:
            title = f"{self._area}: Cases over time"
        self.line_plot(df=df, title=title, y_integer=True, **kwargs)
        return df.reset_index()

    def records_diff(self, variables=None, window=7, **kwargs):
        """
        Return the number of daily new cases (the first discreate difference of records).

        Args:
            variables (list[str] or str or None): variable names or abbreviated names (as the same as Scenario.records())
            window (int): window of moving average, >= 1
            kwargs: the other keyword arguments of Scenario.line_plot()

        Returns:
            pandas.DataFrame
                Index
                    - Date (pd.Timestamp): Observation date
                Columns
                    - Confirmed (int): daily new cases of Confirmed, if calculated
                    - Infected (int):  daily new cases of Infected, if calculated
                    - Fatal (int):  daily new cases of Fatal, if calculated
                    - Recovered (int):  daily new cases of Recovered, if calculated
        """
        window = self._ensure_natural_int(window, name="window")
        df = self.records(variables=variables, show_figure=False).set_index(self.DATE)
        df = df.diff().dropna()
        df = df.rolling(window=window).mean().dropna().astype(np.int64)
        if self._data.complemented:
            title = f"{self._area}: Daily new cases\nwith {self._data.complemented}"
        else:
            title = f"{self._area}: Daily new cases"
        self.line_plot(df=df, title=title, y_integer=True, **kwargs)
        return df

    def _init_trackers(self):
        """
        Initialize dictionary of trackers.
        """
        tracker = PhaseTracker(self._data.records_main(), self.today, self._area)
        self._tracker_dict = {self.MAIN: tracker}

    def _tracker(self, name, template="Main"):
        """
        Ensure that the phases series is registered.
        If not registered, copy the template phase series.

        Args:
            name (str): phase series name
            template (str): name of template phase series

        Returns:
            covsirphy.ParamTracker

        Note:
            If regressors was registered by Scenario.fit(), the regressor will be removed.
        """
        # Registered
        if name in self._tracker_dict:
            return self._tracker_dict[name]
        # Create it, if un-registered
        if template not in self._tracker_dict:
            raise ScenarioNotFoundError(template)
        tracker = copy.deepcopy(self._tracker_dict[template])
        self._tracker_dict[name] = tracker
        # Remove regressor
        if name in self._reghandler_dict:
            self._reghandler_dict.pop(name)
        return tracker

    @deprecate(old="Scenario.add_phase()", new="Scenario.add()")
    def add_phase(self, **kwargs):
        return self.add(**kwargs)

    def add(self, name="Main", end_date=None, days=None, model=None, tau=None, **kwargs):
        """
        Add a new phase.
        The start date will be the next date of the last registered phase.

        Args:
            name (str): phase series name, 'Main' or user-defined name
            end_date (str): end date of the new phase
            days (int): the number of days to add
            model (covsirphy.ModelBase or None): ODE model or None (not specified here)
            tau (int or None): tau value [min] or None (not specified here)
            kwargs: keyword arguments of ODE model parameters, not including tau value.

        Raises:
            ValueError: @end_date if smaller than the last end date of registered phases
            ValueError: no ODE models were registered, but @model was not specified when we have kwargs
            ValueError: tau value was not registered, but @tau was not specified when we have kwargs
            KeyError: some of parameter values were not specified in kwargs (must be all or noting)

        Returns:
            covsirphy.Scenario: self

        Note:
            If @end_date and @days are None, the end date will be the last date of the records.

        Note:
            When registered, ODE model and tau value will not be updated by @model and @tau.

        Note:
            If both of @end_date and @days were specified, @end_date will be used.
        """
        # Get tracker
        tracker = self._tracker(name)
        # Calculate start/end date
        summary_df = tracker.summary()
        if summary_df.empty:
            last_end = self._ensure_date(self._data.first_date) - timedelta(days=1)
        else:
            last_end = summary_df[self.END].max()
        if end_date is None and days is not None:
            days = self._ensure_natural_int(days, name="days")
            end = last_end + timedelta(days=days)
        else:
            today = pd.to_datetime(self._data.today)
            end = self._ensure_date(end_date, name="end_date", default=today)
        if end <= last_end:
            last_end_date = last_end.strftime(self.DATE_FORMAT)
            raise ValueError(
                f"@end_date must be over {last_end_date}. However, {end_date} was applied.")
        # Add phase
        start = last_end + timedelta(days=1)
        tracker.define_phase(start, end)
        if not kwargs:
            self._tracker_dict[name] = tracker
            return self
        # Set ODE model and tau value, if necessary
        if self._model is None and model is None:
            raise ValueError(
                "@model must be specified, because no ODE models were registered and we have kwargs.")
        self._model = self._ensure_subclass(model or self._model, ModelBase, name="model")
        self._tau = self._tau or self._ensure_tau(tau)
        if self._tau is None:
            raise ValueError(
                "@tau must be specified, because tau value was not registered and we have kwargs.")
        # Set ODE parameter values
        self._ensure_kwargs(self._model.PARAMETERS, float, **kwargs)
        param_df = pd.DataFrame(index=pd.date_range(start, end))
        for (param, value) in kwargs.items():
            param_df[param] = value
        tracker.set_ode(self._model, param_df, self._tau)
        # Update tracker of self
        self._tracker_dict[name] = tracker
        return self

    def clear(self, name="Main", include_past=False, template="Main"):
        """
        Clear phase information.

        Args:
            name (str): scenario name
            include_past (bool): if True, past phases will be removed as well as future phases
            template (str): name of template scenario

        Returns:
            covsirphy.Scenario: self

        Note:
            If un-registered scenario name was specified, new scenario will be created.
            Future phases will be always deleted.
        """
        tracker = self._tracker(name, template=template)
        if include_past:
            self[name] = tracker.remove_phase(self._data.first_date, self._data.last_date)
        else:
            df = tracker.summary()
            future_phases = df.loc[df[self.TENSE] == self.FUTURE].index.tolist()
            dates = tracker.phase_to_dates(phases=future_phases)
            self[name] = tracker.remove_phase(min(dates), max(dates))
        return self

    def _delete_series(self, name):
        """
        Delete a scenario or initialise main scenario.

        Args:
            name (str): name of phase series

        Returns:
            covsirphy.Scenario: self
        """
        if name == self.MAIN:
            self[self.MAIN] = self._tracker(self.MAIN).delete_all()
        else:
            self._tracker_dict.pop(name)
        return self

    def delete(self, phases=None, name="Main"):
        """
        Delete phases.

        Args:
            phase (list[str] or None): phase names, or ['last']
            name (str): name of phase series

        Returns:
            covsirphy.Scenario: self

        Note:
            If @phases is None, the phase series will be deleted.
            When @phase is '0th', disable 0th phase. 0th phase will not be deleted.
            If the last phase is included in @phases, the dates will be released from phases.
            If the last phase is not included, the dates will be assigned to the previous phase.
        """
        # Clear main series or delete sub phase series
        if phases is None:
            return self._delete_series(name)
        # Delete phases
        tracker = self._tracker(name)
        last_phase = tracker.summary().index[-1]
        if "last" in phases:
            phases = [last_phase if ph == "last" else ph for ph in phases]
        dates = tracker.phase_to_date(phases=phases)
        if last_phase in phases:
            self[name] = tracker.deactivate(min(dates), max(dates))
        else:
            self[name] = tracker.remove_phase(min(dates), max(dates))
        return self

    def disable(self, phases, name="Main"):
        """
        The phases will be disabled and removed from summary.

        Args:
            phase (list[str] or None): phase names or None (all enabled phases)
            name (str): scenario name

        Returns:
            covsirphy.Scenario: self
        """
        tracker = self._tracker(name)
        last_phase = tracker.summary().index[-1]
        if "last" in phases:
            phases = [last_phase if ph == "last" else ph for ph in phases]
        dates = tracker.phase_to_date(phases=phases)
        self[name] = tracker.deactivate(min(dates), max(dates))
        return self

    def enable(self, phases, name="Main"):
        """
        The phases will be enabled and appear in summary.

        Args:
            phase (list[str] or None): phase names or None (all disabled phases)
            name (str): scenario name

        Returns:
            covsirphy.Scenario: self
        """
        tracker = self._tracker(name)
        last_phase = tracker.summary().index[-1]
        if "last" in phases:
            phases = [last_phase if ph == "last" else ph for ph in phases]
        dates = tracker.phase_to_date(phases=phases)
        self[name] = tracker.define_phase(min(dates), max(dates))
        return self

    def combine(self, phases, name="Main", **kwargs):
        """
        Combine the sequential phases as one phase.
        New phase name will be automatically determined.

        Args:
            phases (list[str]): list of phases
            name (str, optional): name of phase series
            kwargs: keyword arguments of parameters

        Note:
            kwargs will be ignore when model and tau is not registered.

        Raises:
            TypeError: @phases is not a list

        Returns:
            covsirphy.Scenario: self
        """
        self._ensure_list(phases, name="phases")
        tracker = self._tracker(name)
        last_phase = tracker.summary().index[-1]
        if "last" in phases:
            phases = [last_phase if ph == "last" else ph for ph in phases]
        dates = tracker.phase_to_date(phases)
        tracker.define_phase(min(dates), max(dates))
        if kwargs and self._model is not None and self._tau is not None:
            kwargs = self._ensure_kwargs(self._model.PARAMETERS, float, **kwargs)
            param_df = pd.DataFrame(index=dates)
            for (param, value) in kwargs.items():
                param_df[param] = value
            tracker.set_ode(self._model, param_df, self._tau)
        self[name] = tracker
        return self

    def separate(self, date, name="Main", **kwargs):
        """
        Create a new phase with the change point.
        New phase name will be automatically determined.

        Args:
            date (pandas.Timestamp or str): change point, i.e. start date of the new phase
            name (str): scenario name
            kwargs: will be ignored

        Returns:
            covsirphy.Scenario: self
        """
        date = self._ensure_date(date, name="date")
        tracker = self._tracker(name)
        df = tracker.summary()
        start_date = df.loc[df[self.START] < date, self.START].max()
        end_date = df.loc[df[self.END] > date, self.START].min()
        tracker.define_phase(start_date, date - timedelta(days=1))
        tracker.define_phase(date, end_date)
        self[name] = tracker
        return self

    def _summary(self, name=None):
        """
        Summarize the series of phases and return a dataframe.

        Args:
            name (str): phase series name
                - name of alternative phase series registered by Scenario.add()
                - if None, all phase series will be shown

        Returns:
            pandas.DataFrame:
                - if @name not None, as the same as PhaseTracker.summary()
                - if @name is None, index will be phase series name and phase name

        Note:
            If 'Main' was used as @name, main PhaseSeries will be used.
        """
        name = self.MAIN if len(self._tracker_dict) == 1 else name
        if name is not None:
            return self._tracker_dict[name].summary()
        dataframes = []
        for (_name, tracker) in self._tracker_dict.items():
            df = tracker.summary().rename_axis(self.PHASE)
            df[self.SERIES] = _name
            dataframes.append(df.reset_index())
        df = pd.concat(dataframes, ignore_index=True, sort=False)
        return df.set_index([self.SERIES, self.PHASE])

    def summary(self, columns=None, name=None):
        """
        Summarize the series of phases and return a dataframe.

        Args:
            name (str): phase series name
                - name of alternative phase series registered by Scenario.add()
                - if None, all phase series will be shown
            columns (list[str] or None): columns to show

        Returns:
            pandas.DataFrame:
            - if @name not None, as the same as PhaseTracker().summary()
            - if @name is None, index will be phase series name and phase name

        Note:
            If 'Main' was used as @name, main PhaseSeries will be used.

        Note:
            If @columns is None, all columns will be shown.

        Note:
            "Start" and "End" are string at this time.
        """
        df = self._summary(name=name).dropna(how="all", axis=1).fillna(self.UNKNOWN)
        df[self.START] = df[self.START].dt.strftime(self.DATE_FORMAT)
        df[self.END] = df[self.END].dt.strftime(self.DATE_FORMAT)
        if columns is None:
            return df
        self._ensure_list(columns, df.columns.tolist(), name="columns")
        return df.loc[:, columns]

    def trend(self, min_size=None, force=True, name="Main", show_figure=True, filename=None, **kwargs):
        """
        Perform S-R trend analysis and set phases.

        Args:
            min_size (int or None): minimum value of phase length [days] (over 2) or None (equal to max of 7 and delay period)
            force (bool): if True, change points will be over-written
            name (str): phase series name
            show_figure (bool): if True, show the result as a figure
            filename (str): filename of the figure, or None (display)
            kwargs: keyword arguments of covsirphy.TrendDetector(), .TrendDetector.sr() and .trend_plot()

        Returns:
            covsirphy.Scenario: self

        Note:
            If @min_size is None, this will be thw max value of 7 days and delay period calculated with .estimate_delay() method.
        """
        # Arguments
        force = kwargs.pop("set_phases", force)
        # Minimum size of phases
        if min_size is None:
            try:
                delay, _ = self.estimate_delay(**find_args(self.estimate_delay, **kwargs))
            except KeyError:
                # Extra datasets are not registered
                delay = 7
            min_size = max(7, delay)
        self._ensure_int_range(min_size, name="min_size", value_range=(2, None))
        kwargs["min_size"] = min_size
        # S-R trend analysis
        tracker = self._tracker(name)
        if not self._interactive and filename is None:
            show_figure = False
        filename = None if self._interactive else filename
        self[name] = tracker.trend(force=force, show_figure=show_figure, filename=filename, **kwargs)
        # Disable 0th phase, if necessary
        if "include_init_phase" in kwargs:
            warnings.warn(
                "@include_init_phase was deprecated. Please use Scenario.disable('0th').",
                DeprecationWarning, stacklevel=2)
            self[name] = tracker.disable(phases=["0th"])
        return self

    def estimate(self, model, phases=None, name="Main", **kwargs):
        """
        Perform parameter estimation for each phases.

        Args:
            model (covsirphy.ModelBase): ODE model
            phases (list[str]): list of phase names, like 1st, 2nd...
            name (str): phase series name
            kwargs: keyword arguments of ODEHander(), ODEHandler.estimate_tau() and .estimate_param()

        Note:
            If @name phase was not registered, new tracker will be created.

        Note:
            If @phases is None, all past phase will be used.
        """
        tracker = self._tracker(name)
        dates = tracker.phase_to_date(phases=phases)
        if phases is not None:
            tracker.deactivate(min(dates), max(dates))
        self._model = self._ensure_subclass(model, ModelBase, name="model")
        self._tau = tracker.estimate(self._model, tau=self._tau, **kwargs)
        self[name] = tracker

    @deprecate("Scenario.phase_estimator()", version="2.19.1-delta-fu1")
    def phase_estimator(self, **kwargs):
        """
        Deprecated. Return the estimator of the phase.

        Raises:
            NotImplementedError
        """
        raise NotImplementedError

    @deprecate("Scenario.estimate_history()", version="2.19.1-delta-fu1")
    def estimate_history(self, **kwargs):
        """
        Deprecated. Show the history of optimization.

        Raises:
            NotImplementedError
        """
        raise NotImplementedError

    def estimate_accuracy(self, phase, name="Main", **kwargs):
        """
        Show the accuracy as a figure.

        Args:
            phase (str): phase name, like 1st, 2nd...
            name (str): phase series name
            kwargs: keyword arguments of covsirphy.Estimator.accuracy()

        Note:
            If 'Main' was used as @name, main PhaseSeries will be used.
        """
        variables = [self.CI, self.F, self.R]
        records_df = self._records(variables=variables)
        tracker = self._tracker(name=name)
        sim_df = tracker.simulate()
        dates = tracker.phase_to_date(phases=[phase])
        df = records_df.merge(sim_df, on=self.DATE, suffixes=("_actual", "_simulated"))
        df = df.set_index(self.DATE).loc[min(dates):max(dates)]
        compare_plot(df, variables=variables, groups=["actual", "simulated"])

    def simulate(self, variables=None, phases=None, name="Main", **kwargs):
        """
        Simulate ODE models with set/estimated parameter values and show it as a figure.

        Args:
            variables (list[str] or str or None): variable names or abbreviated names (as the same as Scenario.records())
            phases (list[str] or None): phases to shoe or None (all phases)
            name (str): phase series name. If 'Main', main PhaseSeries will be used
            kwargs: the other keyword arguments of Scenario.line_plot()

        Returns:
            pandas.DataFrame
                Index
                    reset index
                Columns
                    - Date (pandas.Timestamp): Observation date
                    - Country (str): country/region name
                    - Province (str): province/prefecture/state name
                    - Variables of the main dataset (int): Confirmed etc.
        """
        tracker = self._tracker(name=name)
        sim_df = tracker.simulate()
        dates = tracker.phase_to_date(phases=phases)
        sim_df = tracker.simulate().set_index(self.DATE).loc[min(dates):max(dates)]
        # Variables to show
        variables = self._convert_variables(variables, candidates=self.VALUE_COLUMNS)
        # Show figure
        title = f"{self._area}: Simulated number of cases ({name} scenario)"
        self.line_plot(
            df=sim_df.loc[:, variables], title=title, y_integer=True, v=tracker.change_dates(), **kwargs)
        return sim_df

    def get(self, param, phase="last", name="Main"):
        """
        Get the parameter value of the phase.

        Args:
            param (str): parameter name (columns in self.summary())
            phase (str): phase name or 'last'
                - if 'last', the value of the last phase will be returned
            name (str): phase series name

        Returns:
            str or int or float

        Note:
            If 'Main' was used as @name, main PhaseSeries will be used.
        """
        df = self.summary(name=name)
        if param not in df.columns:
            raise KeyError(f"@param must be in {', '.join(df.columns)}.")
        if phase == "last":
            phase = df.index[-1]
        return df.loc[phase, param]

    @deprecate(
        old="Scenario.param_history(targets: list)",
        new="Scenario.history(target: str)",
        version="2.7.3-alpha")
    def param_history(self, **kwargs):
        """
        Deprecated. Return subset of summary and show a figure to show the history.

        Raises:
            NotImplementedError
        """
        raise NotImplementedError

    def adjust_end(self):
        """
        Adjust the last end dates of the registered scenarios, if necessary.

        Returns:
            covsirphy.Scenario: self
        """
        # The current last end dates
        current_dict = {
            name: tracker.summary()[self.END].max()
            for (name, tracker) in self._tracker_dict.items()}
        # Adjusted end date
        adjusted = max(current_dict.values())
        for (name, _) in self._tracker_dict.items():
            try:
                self.add(end_date=adjusted, name=name)
            except ValueError:
                pass
        return self

    def _describe(self):
        """
        Describe representative values.

        Returns:
            pandas.DataFrame
                Index
                    (int): scenario name
                Columns
                    - max(Infected): max value of Infected
                    - argmax(Infected): the date when Infected shows max value
                    - Confirmed({date}): Confirmed on the next date of the last phase
                    - Infected({date}): Infected on the next date of the last phase
                    - Fatal({date}): Fatal on the next date of the last phase
        """
        _dict = {}
        for (name, _) in self._tracker_dict.items():
            # Predict the number of cases
            df = self.simulate(name=name, show_figure=False)
            df = df.set_index(self.DATE)
            cols = df.columns[:]
            last_date = df.index[-1]
            # Max value of Infected
            max_ci = df[self.CI].max()
            argmax_ci = df[self.CI].idxmax().strftime(self.DATE_FORMAT)
            # Confirmed on the next date of the last phase
            last_c = df.loc[last_date, self.C]
            # Infected on the next date of the last phase
            last_ci = df.loc[last_date, self.CI]
            # Fatal on the next date of the last phase
            last_f = df.loc[last_date, self.F] if self.F in cols else None
            # Save representative values
            last_date_str = last_date.strftime(self.DATE_FORMAT)
            _dict[name] = {
                f"max({self.CI})": max_ci,
                f"argmax({self.CI})": argmax_ci,
                f"{self.C} on {last_date_str}": last_c,
                f"{self.CI} on {last_date_str}": last_ci,
                f"{self.F} on {last_date_str}": last_f,
            }
        return pd.DataFrame.from_dict(_dict, orient="index")

    def describe(self, with_rt=True, **kwargs):
        """
        Describe representative values.

        Args:
            with_rt (bool): whether show the history of Rt values
            kwargs: the other arguments will be ignored

        Returns:
            pandas.DataFrame:
                Index
                    str: scenario name
                Columns
                    - max(Infected): max value of Infected
                    - argmax(Infected): the date when Infected shows max value
                    - Confirmed({date}): Confirmed on the next date of the last phase
                    - Infected({date}): Infected on the next date of the last phase
                    - Fatal({date}): Fatal on the next date of the last phase
                    - nth_Rt etc.: Rt value if the values are not the same values
        """
        df = self._describe()
        if not with_rt or len(self._tracker_dict) == 1:
            return df
        # History of reproduction number
        rt_df = self.summary().reset_index()
        rt_df = rt_df.pivot_table(index=self.SERIES, columns=self.PHASE, values=self.RT)
        rt_df = rt_df.fillna(self.UNKNOWN)
        rt_df = rt_df.loc[:, rt_df.nunique() > 1]
        cols = sorted(rt_df, key=self.str2num)
        return df.join(rt_df[cols].add_suffix(f"_{self.RT}"), how="left")

    def track(self, phases=None, with_actual=True, **kwargs):
        """
        Show values of parameters and variables in one dataframe.

        Args:
            phases (list[str] or None): phases to shoe or None (all phases)
            with_actual (bool): if True, show actual number of cases will included as "Actual" scenario
            kwargs: the other arguments will be ignored

        Returns:
            pandas.DataFrame: tracking records
                Index
                    reset index
                Columns
                    - Scenario (str)
                    - Date (pandas.TimeStamp)
                    - variables (int)
                    - Population (int)
                    - Rt (float)
                    - parameter values (float)
                    - day parameter values (float)
        """
        dataframes = []
        append = dataframes.append
        for name in self._tracker_dict.keys():
            df = self._tracker(name=name).track()
            df = df.drop([self.S, self.C, self.CI, self.F, self.R], axis=1)
            df.insert(0, self.SERIES, name)
            append(df)
        if with_actual:
            df = self._data.records(extras=False)
            df.insert(0, self.SERIES, self.ACTUAL)
            append(df)
        track_df = pd.concat(dataframes, axis=0, sort=False)
        dates = self._tracker(name=self.MAIN).phase_to_date(phases=phases)
        return track_df.loc[(min(dates) < track_df[self.DATE]) & (track_df[self.DATE] <= max(dates))]

    def _history(self, target, phases=None, with_actual=True):
        """
        Show the history of variables and parameter values to compare scenarios.

        Args:
            target (str): parameter or variable name to show (Rt, Infected etc.)
            phases (list[str] or None): phases to shoe or None (all phases)
            with_actual (bool): if True and @target is a variable name, show actual number of cases

        Returns:
            pandas.DataFrame
        """
        # Include actual data or not
        with_actual = with_actual and target in self.VALUE_COLUMNS
        # Get tracking data
        df = self.track(phases=phases, with_actual=with_actual)
        if target not in df.columns:
            col_str = ", ".join(list(df.columns))
            raise KeyError(f"@target must be selected from {col_str}, but {target} was applied.")
        # Select the records of target variable
        return df.pivot_table(
            values=target, index=self.DATE, columns=self.SERIES, aggfunc="last")

    def history(self, target, phases=None, with_actual=True, **kwargs):
        """
        Show the history of variables and parameter values to compare scenarios.

        Args:
            target (str): parameter or variable name to show (Rt, Infected etc.)
            phases (list[str] or None): phases to shoe or None (all phases)
            with_actual (bool): if True and @target is a variable name, show actual number of cases
            kwargs: the other keyword arguments of Scenario.line_plot()

        Returns:
            pandas.DataFrame
        """
        df = self._history(target=target, phases=phases, with_actual=with_actual)
        df.dropna(subset=[col for col in df.columns if col != self.ACTUAL], inplace=True)
        if target == self.RT:
            ylabel = self.RT_FULL
        elif target in self.VALUE_COLUMNS:
            ylabel = f"The number of {target.lower()} cases"
        else:
            ylabel = target
        title = f"{self._area}: {ylabel} over time"
        tracker = self._tracker(self.MAIN)
        self.line_plot(
            df=df, title=title, ylabel=ylabel, v=tracker.change_dates(), math_scale=False,
            h=1.0 if target == self.RT else None, **kwargs)
        return df

    def history_rate(self, params=None, name="Main", **kwargs):
        """
        Show change rates of parameter values in one figure.
        We can find the parameters which increased/decreased significantly.

        Args:
            params (list[str] or None): parameters to show
            name (str): phase series name
            kwargs: the other keyword arguments of Scenario.line_plot()

        Returns:
            pandas.DataFrame
        """
        df = self._tracker(name=name).track()
        cols = list(set(df.columns) & set(self._model.PARAMETERS))
        if params is not None:
            if not isinstance(params, (list, set)):
                raise TypeError(f"@params must be a list of parameters, but {params} were applied.")
            cols = list(set(cols) & set(params)) or cols
        df = df.loc[:, cols] / df.loc[df.index[0], cols]
        # Show figure
        f_date = df.index[0].strftime(self.DATE_FORMAT)
        title = f"{self._area}: {self._model.NAME} parameter change rates over time (1.0 on {f_date})"
        ylabel = f"Value per that on {f_date}"
        title = f"{self._area}: {ylabel} over time"
        tracker = self._tracker(self.MAIN)
        self.line_plot(
            df=df, title=title, ylabel=ylabel, v=tracker.change_dates(), math_scale=False, **kwargs)
        return df

    def retrospective(self, beginning_date, model, control="Main", target="Target", **kwargs):
        """
        Perform retrospective analysis.
        Compare the actual series of phases (control) and
        series of phases with specified parameters (target).

        Args:
            beginning_date (str): when the parameter values start to be changed from actual values
            model (covsirphy.ModelBase): ODE model
            control (str): scenario name of control
            target (str): scenario name of target
            kwargs: keyword arguments of ODEHander(), ODEHandler.estimate_tau() and .estimate_param()

        Note:
            When parameter values are not specified,
            actual values of the last date before the beginning date will be used.
        """
        param_dict = {k: v for (k, v) in kwargs.items() if k in model.PARAMETERS}
        # Control
        self.clear(name=control, include_past=True)
        self.trend(name=control, show_figure=False)
        try:
            self.separate(date=beginning_date, name=control)
        except ValueError:
            pass
        self.estimate(model, name=control, **kwargs)
        # Target
        self.clear(name=target, include_past=False, template=control)
        phases_changed = [
            self.num2str(i) for (i, ph) in enumerate(self._tracker(target).series)
            if ph >= beginning_date]
        self.delete(phases=phases_changed, name=target)
        self.add(name=target, **param_dict)
        self.estimate(model, name=target, **kwargs)

    def score(self, variables=None, phases=None, past_days=None, name="Main", **kwargs):
        """
        Evaluate accuracy of phase setting and parameter estimation of all enabled phases all some past days.

        Args:
            variables (list[str] or None): variables to use in calculation
            phases (list[str] or None): phases to use in calculation or None (all phases)
            past_days (int or None): how many past days to use in calculation, natural integer
            name(str): phase series name. If 'Main', main PhaseSeries will be used
            kwargs: keyword arguments of covsirphy.Evaluator.score()

        Returns:
            float: score with the specified metrics (covsirphy.Evaluator.score())

        Note:
            If @variables is None, ["Infected", "Fatal", "Recovered"] will be used.

        Note:
            "Susceptible", "Confirmed", "Infected", "Fatal" and "Recovered" can be used in @variables.

        Note:
            @phases and @past_days can not be specified at the same time.

        Note:
            Please refer to covsirphy.Evaluator.score() for metrics.
        """
        variables = self._ensure_list(
            variables or [self.CI, self.F, self.R],
            candidates=[self.S, self.C, self.CI, self.F, self.R], name="variables")
        tracker = self._tracker(name)
        sim_df = tracker.simulate().set_index(self.DATE)
        rec_df = self.records(variables=variables).set_index(self.DATE)
        if past_days is not None and phases is not None:
            raise ValueError("@phases and @past_days cannot be specified at the same time.")
        if phases is not None:
            dates = tracker.phase_to_date(phases=phases)
            sim_df = sim_df.loc[min(dates):max(dates)]
            rec_df = rec_df.loc[min(dates):max(dates)]
        if past_days is not None:
            past_days = self._ensure_natural_int(past_days, name="past_days")
            # Separate a phase, if possible
            today = self._ensure_date(self._today)
            beginning_date = today - timedelta(days=past_days)
            sim_df = sim_df.loc[beginning_date:today]
            rec_df = rec_df.loc[beginning_date:today]
        evaluator = Evaluator(rec_df, sim_df, how="inner")
        return evaluator.score(**find_args(Evaluator.score, **kwargs))

    def estimate_delay(self, oxcgrt_data=None, indicator="Stringency_index",
                       target="Confirmed", percentile=25, limits=(7, 30), **kwargs):
        """
        Estimate delay period [days], assuming the indicator impact on the target value with delay.
        The average of representative value (percentile) and @min_size will be returned.

        Args:
            oxcgrt_data (covsirphy.OxCGRTData): OxCGRT dataset
            indicator (str): indicator name, a column of any registered datasets
            target (str): target name, a column of any registered datasets
            percentile (int): percentile to calculate the representative value, in (0, 100)
            limits (tuple(int, int)): minimum/maximum size of the delay period [days]
            kwargs: keyword arguments of DataHandler.estimate_delay()

        Raises:
            NotRegisteredMainError: either JHUData or PopulationData was not registered
            SubsetNotFoundError: failed in subsetting because of lack of data
            UserWarning: failed in calculating and returned the default value (recovery period)

        Returns:
            tuple(int, pandas.DataFrame):
                - int: the estimated number of days of delay [day] (mode value)
                - pandas.DataFrame:
                    Index
                        reset index
                    Columns
                        - (int or float): column defined by @indicator
                        - (int or float): column defined by @target
                        - (int): column defined by @delay_name [days]

        Note:
            - Average recovered period of JHU dataset will be used as returned value when the estimated value was not in value_range.
            - @oxcgrt_data argument was deprecated. Please use Scenario.register(extras=[oxcgrt_data]).
        """
        min_size, max_days = limits
        # Register OxCGRT data
        if oxcgrt_data is not None:
            warnings.warn(
                "Please use Scenario.register(extras=[oxcgrt_data]) rather than Scenario.fit(oxcgrt_data).",
                DeprecationWarning, stacklevel=1)
            self.register(extras=[oxcgrt_data])
        # Un-used arguments
        if "value_range" in kwargs:
            warnings.warn("@value_range argument was deprecated.", DeprecationWarning, stacklevel=1)
        # Calculate delay values
        df = self._data.estimate_delay(
            indicator=indicator, target=target, min_size=min_size, delay_name="Period Length",
            **find_args(DataHandler.estimate_delay, **kwargs))
        # Remove NAs and sort
        df.dropna(subset=["Period Length"], inplace=True)
        df.sort_values("Period Length", inplace=True)
        df.reset_index(inplace=True, drop=True)
        # Apply upper limit for delay period if max_days is set
        if max_days is not None:
            df = df[df["Period Length"] <= max_days]
        # Calculate representative value
        if df.empty:
            return (self._data.recovery_period(), df)
        # Calculate percentile
        Q1 = np.percentile(df["Period Length"], percentile, interpolation="midpoint")
        low_lim = min_size
        delay_period = int((low_lim + Q1) / 2)
        return (int(delay_period), df)

    def fit(self, oxcgrt_data=None, name="Main", delay=None, removed_cols=None, metric=None, metrics="R2", **kwargs):
        """
        Fit regressors to predict the parameter values in the future phases,
        assuming that indicators will impact on ODE parameter values/the number of cases with delay.
        Please refer to covsirphy.RegressionHander class.

        Args:
            oxcgrt_data (covsirphy.OxCGRTData): OxCGRT dataset, deprecated
            name (str): scenario name
            test_size (float): proportion of the test dataset of Elastic Net regression
            seed (int): random seed when spliting the dataset to train/test data
            delay (int or tuple(int, int) or None):
                - (int): delay period [days],
                - tuple(int, int): select the best value with grid search in this range, or
                - None: Scenario.estimate_delay() calculate automatically
            removed_cols (list[str] or None): list of variables to remove from X dataset or None (indicators used to estimate delay period)
            metric (str or None): metric name or None (use @metrics)
            metrics (str): alias of @metric
            kwargs: keyword arguments of sklearn.model_selection.train_test_split(test_size=0.2, random_state=0)

        Raises:
            covsirphy.UnExecutedError: Scenario.estimate() or Scenario.add() were not performed

        Returns:
            dict: this is the same as covsirphy.Regressionhander.to_dict()

        Note:
            @oxcgrt_data argument was deprecated. Please use Scenario.register(extras=[oxcgrt_data]).

        Note:
            Please refer to covsirphy.Evaluator.score() for metric names.

        Note:
            If @seed is included in kwargs, this will be converted to @random_state.
        """
        metric = metric or metrics
        # Clear the future phases
        self.clear(name=name, include_past=False)
        # Register OxCGRT data
        if oxcgrt_data is not None:
            warnings.warn(
                "Please use Scenario.register(extras=[oxcgrt_data]) rather than Scenario.fit(oxcgrt_data).",
                DeprecationWarning, stacklevel=1)
            self.register(extras=[oxcgrt_data])
        # ODE model
        if self._model is None:
            raise UnExecutedError(
                "Scenario.estimate() or Scenario.add()",
                message=f", specifying @model (covsirphy.SIRF etc.) and @name='{name}'.")
        # Create training/test dataset
        param_df = self._tracker(name=name).track()[self._model.PARAMETERS]
        try:
            records_df = self._data.records(main=True, extras=True).set_index(self.DATE)
        except NotRegisteredExtraError:
            raise NotRegisteredExtraError(
                "Scenario.register(jhu_data, population_data, extras=[...])",
                message="with extra datasets") from None
        records_df = records_df.loc[:, ~records_df.columns.isin(removed_cols or [])]
        data = param_df.join(records_df)
        # Set delay effect
        if delay is None:
            # Estimate delay period with Scenario.estimate_delay()
            delay, delay_df = self.estimate_delay(oxcgrt_data)
            records_df = records_df.loc[:, ~records_df.columns.isin(delay_df.columns)]
            data = param_df.join(records_df)
        elif isinstance(delay, tuple):
            # Estimate the best delay value with grid search in the range
            delay_min, delay_max = delay
            self._ensure_natural_int(delay_min, name="delay[0]")
            self._ensure_natural_int(delay_max, name="delay[1]")
            score_dict = {}
            for candidate in range(delay_min, delay_max + 1):
                handler_candidate = RegressionHandler(
                    data=data, model=self._model, delay=candidate, **kwargs)
                try:
                    score_dict[candidate] = handler_candidate.fit(metric=metric)
                except ValueError:
                    pass
            delay, _ = Evaluator.best_one(score_dict, metric=metric)
        else:
            # Use specified delay value
            delay = self._ensure_natural_int(delay, name="delay")
        # Fit regression models
        handler = RegressionHandler(data=data, model=self._model, delay=delay, **kwargs)
        handler.fit(metric=metric)
        self._reghandler_dict[name] = handler
        # Return information
        return handler.to_dict(metric=metric)

    def predict(self, days=None, name="Main"):
        """
        Predict parameter values of the future phases using Elastic Net regression with OxCGRT scores,
        assuming that OxCGRT scores will impact on ODE parameter values with delay.
        New future phases will be added (over-written).

        Args:
            days (list[int]): list of days to predict or None (only the max value)
            name (str): scenario name

        Raises:
            covsirphy.UnExecutedError: Scenario.fit() was not performed
            NotRegisteredExtraError: no extra datasets were registered

        Returns:
            covsirphy.Scenario: self
        """
        # Arguments
        if name not in self._reghandler_dict:
            raise UnExecutedError(f"Scenario.fit(name={name})")
        # Prediction with regression model
        handler = self._reghandler_dict[name]
        df = handler.predict()
        # -> end_date/parameter values
        df.index = [date.strftime(self.DATE_FORMAT) for date in df.index]
        df.index.name = "end_date"
        # Days to predict
        days = days or [len(df) - 1]
        self._ensure_list(days, candidates=list(range(len(df))), name="days")
        phase_df = df.reset_index().loc[days, :]
        # Set new future phases
        for phase_dict in phase_df.to_dict(orient="records"):
            self.add(name=name, **phase_dict)
        return self

    def fit_predict(self, oxcgrt_data=None, name="Main", **kwargs):
        """
        Predict parameter values of the future phases using Elastic Net regression with OxCGRT scores,
        assuming that OxCGRT scores will impact on ODE parameter values with delay.
        New future phases will be added (over-written).

        Args:
            oxcgrt_data (covsirphy.OxCGRTData or None): OxCGRT dataset
            name (str): scenario name
            kwargs: the other arguments of Scenario.fit() and Scenario.predict()

        Raises:
            covsirphy.UnExecutedError: Scenario.estimate() or Scenario.add() were not performed
            NotRegisteredExtraError: no extra datasets were registered

        Returns:
            covsirphy.Scenario: self

        Note:
            @oxcgrt_data argument was deprecated. Please use Scenario.register(extras=[oxcgrt_data]).
        """
        self.fit(oxcgrt_data=oxcgrt_data, name=name, **find_args(Scenario.fit, **kwargs))
        self.predict(name=name, **find_args(Scenario.predict, **kwargs))
        return self
