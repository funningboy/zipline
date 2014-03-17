import numpy as np
import pandas as pd

from . history import (
    index_at_dt,
    days_index_at_dt,
)

from qexec.sources.history_source import populate_initial_day_panel

from zipline.finance import trading
from zipline.utils.data import RollingPanel

# The closing price is referred to be multiple names,
# allow both for price rollover logic etc.
CLOSING_PRICE_FIELDS = {'price', 'close_price'}


def create_initial_day_panel(days_needed, fields, sids, dt):
    index = days_index_at_dt(days_needed, dt)
    # Use original index in case of 1 bar.
    if days_needed != 1:
        index = index[:-1]
    window = len(index)
    rp = RollingPanel(window, fields, sids)
    for i, day in enumerate(index):
        rp.index_buf[i] = day
    rp.pos = window
    return rp


def create_current_day_panel(fields, sids, dt):
    # Can't use open_and_close since need to create enough space for a full
    # day, even on a half day.
    # Can now use mkt open and close, since we don't roll
    env = trading.environment
    index = env.market_minutes_for_day(dt)
    return pd.Panel(items=fields, minor_axis=sids, major_axis=index)


def ffill_day_frame(field, day_frame, prior_day_frame):
    # get values which are nan-at the beginning of the day
    # and attempt to fill with the last close
    first_bar = day_frame.ix[0]
    nan_sids = first_bar[np.isnan(first_bar)]
    for sid, _ in nan_sids.iterkv():
        day_frame[sid][0] = prior_day_frame.ix[-1, sid]
    if field != 'volume':
        day_frame = day_frame.ffill()
    return day_frame


class HistoryContainer(object):
    """
    Container for all history panels and frames used by an algoscript.

    To be used internally by algoproxy, but *not* passed directly to the
    algorithm.
    Entry point for the algoscript is the result of `get_history`.
    """

    def __init__(self, db, history_specs, initial_sids, initial_dt):

        self.db = db

        # All of the history specs found by the algoscript parsing.
        self.history_specs = history_specs

        # The overaching panel needs to be large enough to contain the
        # largest history spec
        self.max_days_needed = max(spec.days_needed for spec
                                   in history_specs.itervalues())

        # The set of fields specified by all history specs
        self.fields = set(spec.field for spec in history_specs.itervalues())

        self.prior_day_panel = create_initial_day_panel(
            self.max_days_needed, self.fields, initial_sids, initial_dt)

        # The panel should contain values dating before the first algodt.
        # The following call does the 'backfilling' so that `get_history`
        # will return full values on the first `handle_data` call.
        # Backfill not needed if only 1 bar
        # Also, only backfill if a database is available; the main case
        # where there is no database available is during unit testing.
        if self.max_days_needed != 1 and self.db:
            populate_initial_day_panel(self.db,
                                       self.prior_day_panel)

        # This panel contains the minutes for the current day.
        # The value that is used is some sort of aggregation call on the
        # panel, e.g. `sum` for volume, `max` for high, etc.
        self.current_day_panel = create_current_day_panel(
            self.fields, initial_sids, initial_dt)

        # Helps prop up the prior day panel against having a nan, when
        # the data has been seen.
        self.last_known_prior_values = {field: {} for field in self.fields}

        # Populating initial frames here, so that the cost of creating the
        # initial frames does not show up when profiling get_history
        # These frames are cached since mid-stream creation of containing
        # data frames on every bar is expensive.
        self.return_frames = {}

        self.create_return_frames(initial_dt)

    def create_return_frames(self, algo_dt):
        """
        Populates the return frame cache.

        Called during init and at universe rollovers.
        """
        for history_spec in self.history_specs.itervalues():
            index = index_at_dt(history_spec, algo_dt)
            index = pd.to_datetime(index)
            frame = pd.DataFrame(
                index=index,
                columns=map(int, self.current_day_panel.minor_axis.values),
                dtype=np.float64)
            self.return_frames[history_spec] = frame

    def update(self, data, algo_dt):
        """
        Takes the bar at @algo_dt's @data and adds to the current day panel.
        """
        self.check_and_roll(algo_dt)

        fields = self.fields
        field_data = {sid: {field: bar[field] for field in fields}
                      for sid, bar in data.iteritems()
                      if (bar
                          and
                          bar['dt'] == algo_dt
                          and
                          # Only use data which is keyed in the data panel.
                          # Prevents crashes due to custom data.
                          sid in self.current_day_panel.minor_axis)}
        field_frame = pd.DataFrame(field_data)
        self.current_day_panel.ix[:, algo_dt, :] = field_frame.T

    def backfill_sids(self, sid_states, dt):
        """
        backfills data for sids that have entered the universe.

        New sids will not have the data for previous bars, so the data
        needs to be fetched and populated when they enter.
        """
        prior_day_panel = self.prior_day_panel.get_current()
        # Remove the dropped sids, to prevent stale data.
        prior_day_panel = prior_day_panel.drop(sid_states['removed_sids'],
                                               axis=2)
        for sid in sid_states['removed_sids']:
            try:
                del self.last_known_prior_values[sid]
            except KeyError:
                # Better to ask forgiveness, than ask permission.
                pass
        existing_sids = set(prior_day_panel.minor_axis)
        sids_to_add = sid_states['new_sids'] - existing_sids
        if not sids_to_add:
            # If there are no new sids to add, shortcircuit.
            return
        total_sids = sids_to_add.union(existing_sids)
        # Like at the beginning of the backtest, use a panel to collect
        # the backfilled values.
        # This implementation is aggressive/inefficent and gets for *all*
        # sids in the current universe, instead of merging the data.
        # Mainly because this was easier than dealing whith the merge logic,
        # and the rollover occurs at quarter turns, which is relatively rare
        # compared to the minute frequency.
        # If universe changes closer to a daily rate, we may need to find
        # a more efficient solution.
        new_sid_rolling_panel = create_initial_day_panel(
            self.max_days_needed,
            self.fields,
            total_sids,
            dt)
        new_sid_panel = new_sid_rolling_panel.get_current()
        if self.max_days_needed != 1:
            populate_initial_day_panel(self.db, new_sid_rolling_panel)
        self.prior_day_panel = new_sid_rolling_panel
        # Create a fresh current day panel, now using the new universe.
        self.current_day_panel = create_current_day_panel(
            self.fields, new_sid_panel.minor_axis, dt)
        self.create_return_frames(dt)

    def roll(self, roll_dt):
        env = trading.environment
        # This should work for price, but not others, e.g.
        # open.
        # Get the most recent value.
        rolled = pd.DataFrame(
            index=self.current_day_panel.items,
            columns=self.current_day_panel.minor_axis)

        for field in self.fields:
            if field in CLOSING_PRICE_FIELDS:
                # Use the last price.
                prices = self.current_day_panel.ffill().ix[field, -1, :]
                rolled.ix[field] = prices
            elif field == 'open_price':
                # Use the first price.
                opens = self.current_day_panel.ix['open_price', 0, :]
                rolled.ix['open_price'] = opens
            elif field == 'volume':
                # Volume is the sum of the volumes during the
                # course of the day
                volumes = self.current_day_panel.ix['volume'].apply(np.sum)
                rolled.ix['volume'] = volumes
            elif field == 'high':
                # Use the highest high.
                highs = self.current_day_panel.ix['high'].apply(np.max)
                rolled.ix['high'] = highs
            elif field == 'low':
                # Use the lowest low.
                lows = self.current_day_panel.ix['low'].apply(np.min)
                rolled.ix['low'] = lows

            for sid, value in rolled.ix[field].iterkv():
                if not np.isnan(value):
                    try:
                        prior_values = self.last_known_prior_values[field][sid]
                    except KeyError:
                        prior_values = {}
                        self.last_known_prior_values[field][sid] = prior_values
                    prior_values['dt'] = roll_dt
                    prior_values['value'] = value

        self.prior_day_panel.add_frame(roll_dt, rolled)

        # Create a new 'current day' collector.
        next_day = env.next_trading_day(roll_dt)

        if next_day:
            # Only create the next panel if there is a next day.
            # i.e. don't create the next panel on the last day of
            # the backest/current day of live trading.
            self.current_day_panel = create_current_day_panel(
                self.fields,
                # Will break on quarter rollover.
                self.current_day_panel.minor_axis,
                next_day)

    def check_and_roll(self, algo_dt):
        """
        Check whether the algo_dt is at the end of a day.
        If it is, aggregate the day's minute data and store it in the prior
        day panel.
        """
        # Use a while loop to account for illiquid bars.
        while algo_dt > self.current_day_panel.major_axis[-1]:
            roll_dt = self.current_day_panel.major_axis[-1]
            self.roll(roll_dt)

    def get_history(self, history_spec, algo_dt):
        """
        Main API used by the algoscript is mapped to this function.

        Selects from the overarching history panel the valuse for the
        @history_spec at the given @algo_dt.
        """
        field = history_spec.field

        index = index_at_dt(history_spec, algo_dt)
        index = pd.to_datetime(index)

        frame = self.return_frames[history_spec]
        # Overwrite the index.
        # Not worrying about values here since the values are overwritten
        # in the next step.
        frame.index = index

        prior_day_panel = self.prior_day_panel.get_current()
        prior_day_frame = prior_day_panel[field].copy()
        if history_spec.ffill:
            first_bar = prior_day_frame.ix[0]
            nan_sids = first_bar[first_bar.isnull()]
            for sid, _ in nan_sids.iterkv():
                try:
                    if (
                        # Only use prior value if it is before the index,
                        # so that a backfill does not accidentally occur.
                        self.last_known_prior_values[field][sid]['dt'] <=
                            prior_day_frame.index[0]):
                        prior_day_frame[sid][0] =\
                            self.last_known_prior_values[field][sid]['value']
                except KeyError:
                    # Allow case where there is no previous value.
                    # e.g. with leading nans.
                    pass
            prior_day_frame = prior_day_frame.ffill()
        frame.ix[:-1] = prior_day_frame.ix[:]

        # Copy the current day frame, since the fill behavior will mutate
        # the values in the panel.
        current_day_frame = self.current_day_panel[field][:algo_dt].copy()
        if history_spec.ffill:
            current_day_frame = ffill_day_frame(field,
                                                current_day_frame,
                                                prior_day_frame)

        if field == 'volume':
            # This works for the day rollup, i.e. '1d',
            # but '1m' will need to allow for 0 or nan minutes
            frame.ix[algo_dt] = current_day_frame.sum()
        elif field == 'high':
            frame.ix[algo_dt] = current_day_frame.max()
        elif field == 'low':
            frame.ix[algo_dt] = current_day_frame.min()
        elif field == 'open_price':
            frame.ix[algo_dt] = current_day_frame.ix[0]
        else:
            frame.ix[algo_dt] = current_day_frame.ix[algo_dt]

        return frame
