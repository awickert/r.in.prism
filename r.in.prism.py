#!/usr/bin/python3
############################################################################
#
# MODULE:       r.in.prism
#
# AUTHOR(S):    Andrew Wickert
#
# PURPOSE:      Import PRISM gridded climate data into GRASS GIS
#
# COPYRIGHT:    (c) 2026 Andrew Wickert
#
#               This program is free software under the GNU General Public
#               License (>=v2). Read the file COPYING that comes with GRASS
#               for details.
#
#############################################################################

#%module
#% description: Import PRISM gridded climate data (time series or 30-year normals)
#% keyword: raster
#% keyword: import
#% keyword: climate
#% keyword: PRISM
#% keyword: precipitation
#% keyword: temperature
#%end

#%option G_OPT_R_BASENAME_OUTPUT
#%  key: output
#%  label: Base name for output raster maps
#%  required: yes
#%end

#%option
#%  key: variable
#%  type: string
#%  label: Climate variable to import
#%  options: ppt,tmin,tmax,tmean,tdmean,vpdmin,vpdmax
#%  answer: ppt
#%  required: no
#%end

#%option
#%  key: mode
#%  type: string
#%  label: Dataset type
#%  options: timeseries,normals
#%  answer: timeseries
#%  required: no
#%end

#%option
#%  key: frequency
#%  type: string
#%  label: Temporal frequency (timeseries mode only)
#%  options: monthly,daily
#%  answer: monthly
#%  required: no
#%end

#%option
#%  key: start
#%  type: string
#%  label: Start date; YYYY-MM for monthly, YYYY-MM-DD for daily (timeseries mode)
#%  required: no
#%end

#%option
#%  key: end
#%  type: string
#%  label: End date; YYYY-MM for monthly, YYYY-MM-DD for daily (timeseries mode)
#%  required: no
#%end

#%option
#%  key: months
#%  type: string
#%  label: Comma-separated month numbers to import (normals mode; omit for all 12)
#%  description: e.g. 1,2,3 for Jan-Mar; 1–12 valid
#%  required: no
#%end

#%flag
#%  key: t
#%  description: Register output maps as a space-time raster dataset (strds)
#%end

import importlib
import io
import os
import tempfile
import zipfile
import atexit
from datetime import date, datetime, timedelta
from calendar import monthrange

import grass.script as gs

if os.path.exists('/usr/share/proj/proj.db'):
    os.environ['PROJ_DATA'] = '/usr/share/proj'

TMPFILES = []

_TIMESERIES_BASE = 'https://services.nacse.org/prism/data/get/us/4km'
_NORMALS_BASE = 'https://data.prism.oregonstate.edu/normals/us/4km'

_VAR_UNITS = {
    'ppt': 'mm',
    'tmin': 'degrees_Celsius',
    'tmax': 'degrees_Celsius',
    'tmean': 'degrees_Celsius',
    'tdmean': 'degrees_Celsius',
    'vpdmin': 'hPa',
    'vpdmax': 'hPa',
}


def cleanup():
    for f in TMPFILES:
        try:
            os.remove(f)
        except OSError:
            pass


def require_package(import_name, pip_name=None):
    try:
        return importlib.import_module(import_name)
    except ImportError:
        gs.fatal(
            "Python package '{}' is required but not installed.\n"
            "Install with: pip install {}".format(import_name, pip_name or import_name)
        )


def timeseries_url(variable, date_str, frequency):
    """Return PRISM timeseries download URL for a given date string."""
    if frequency == 'monthly':
        # date_str is YYYYMM
        return '{}/{}/{}'.format(_TIMESERIES_BASE, variable, date_str)
    else:
        # date_str is YYYYMMDD
        return '{}/{}/{}'.format(_TIMESERIES_BASE, variable, date_str)


def normals_url(variable, month):
    """Return PRISM 30-year normals URL for a given month (1–12)."""
    mm = '{:02d}'.format(month)
    return '{}/{}/monthly/prism_{}_us_25m_2020{}_avg_30y.zip'.format(
        _NORMALS_BASE, variable, variable, mm
    )


def download_and_extract_tif(url):
    """Download a zip from url, extract the .tif, return path to temp tif file."""
    import requests

    gs.verbose("  Downloading: {}".format(url))
    r = requests.get(url, timeout=120)
    if r.status_code == 404:
        gs.warning("  Not found (404): {}".format(url))
        return None
    r.raise_for_status()

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    tif_names = [n for n in zf.namelist() if n.lower().endswith('.tif')]
    if not tif_names:
        gs.warning("  No .tif found in zip from: {}".format(url))
        return None

    fd, tmp_tif = tempfile.mkstemp(suffix='.tif')
    os.close(fd)
    TMPFILES.append(tmp_tif)

    with zf.open(tif_names[0]) as src, open(tmp_tif, 'wb') as dst:
        dst.write(src.read())

    return tmp_tif


def import_tif(tif_path, map_name):
    """Import a GeoTiff into GRASS, reprojecting + clipping to current region."""
    gs.run_command(
        'r.import',
        input=tif_path,
        output=map_name,
        resample='bilinear',
        overwrite=gs.overwrite(),
        quiet=True,
    )


def monthly_date_range(start, end):
    """Yield YYYYMM strings from start to end inclusive (both YYYY-MM)."""
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    while (y, m) <= (ey, em):
        yield '{}{:02d}'.format(y, m)
        m += 1
        if m > 12:
            m = 1
            y += 1


def daily_date_range(start, end):
    """Yield YYYYMMDD strings from start to end inclusive (both YYYY-MM-DD)."""
    d = datetime.strptime(start, '%Y-%m-%d').date()
    ed = datetime.strptime(end, '%Y-%m-%d').date()
    while d <= ed:
        yield d.strftime('%Y%m%d')
        d += timedelta(days=1)


def register_strds(output, map_names, date_strings, frequency):
    """Create strds and register maps with timestamps."""
    strds_name = output
    gs.run_command(
        't.create',
        output=strds_name,
        type='strds',
        temporaltype='absolute',
        title=output,
        description='PRISM {} imported by r.in.prism'.format(output),
        overwrite=gs.overwrite(),
        quiet=True,
    )

    # Build file list: map|start|end
    fd, reg_file = tempfile.mkstemp(suffix='.txt')
    os.close(fd)
    TMPFILES.append(reg_file)

    with open(reg_file, 'w') as f:
        for map_name, ds in zip(map_names, date_strings):
            if frequency == 'monthly':
                y, m = int(ds[:4]), int(ds[4:6])
                start_dt = '{}-{:02d}-01'.format(y, m)
                last_day = monthrange(y, m)[1]
                end_dt = '{}-{:02d}-{:02d}'.format(y, m, last_day)
            else:
                d = datetime.strptime(ds, '%Y%m%d').date()
                start_dt = d.isoformat()
                end_dt = (d + timedelta(days=1)).isoformat()
            f.write('{}|{}|{}\n'.format(map_name, start_dt, end_dt))

    gs.run_command(
        't.register',
        input=strds_name,
        file=reg_file,
        overwrite=gs.overwrite(),
        quiet=True,
    )


def main():
    options, flags = gs.parser()
    atexit.register(cleanup)

    output = options['output']
    variable = options['variable']
    mode = options['mode']
    frequency = options['frequency']
    start = options['start'] or None
    end = options['end'] or None
    months_str = options['months'] or None
    flag_strds = flags['t']

    require_package('requests')

    map_names = []
    date_strings = []

    if mode == 'timeseries':
        if not start:
            gs.fatal("start= is required for mode=timeseries.")
        if not end:
            end = date.today().strftime('%Y-%m' if frequency == 'monthly' else '%Y-%m-%d')

        if frequency == 'monthly':
            all_dates = list(monthly_date_range(start, end))
        else:
            all_dates = list(daily_date_range(start, end))

        gs.message("Importing {} PRISM {} maps ({})...".format(
            len(all_dates), variable, frequency))

        for ds in all_dates:
            map_name = '{}_{}'.format(output, ds)
            url = timeseries_url(variable, ds, frequency)
            tif = download_and_extract_tif(url)
            if tif is None:
                gs.warning("Skipping {}.".format(ds))
                continue
            import_tif(tif, map_name)
            map_names.append(map_name)
            date_strings.append(ds)
            gs.message("  Imported: {}".format(map_name))

        if flag_strds and map_names:
            gs.message("Registering {} maps in strds '{}'...".format(
                len(map_names), output))
            register_strds(output, map_names, date_strings, frequency)
            gs.message("strds '{}' created.".format(output))

    elif mode == 'normals':
        if months_str:
            months = [int(m.strip()) for m in months_str.split(',')]
            for m in months:
                if not 1 <= m <= 12:
                    gs.fatal("Invalid month: {}. Must be 1-12.".format(m))
        else:
            months = list(range(1, 13))

        gs.message("Importing PRISM 30-year normals for {} ({} months)...".format(
            variable, len(months)))

        for month in months:
            map_name = '{}_{:02d}'.format(output, month)
            url = normals_url(variable, month)
            tif = download_and_extract_tif(url)
            if tif is None:
                gs.warning("Skipping month {:02d}.".format(month))
                continue
            import_tif(tif, map_name)
            map_names.append(map_name)
            date_strings.append('2020{:02d}'.format(month))
            gs.message("  Imported: {}".format(map_name))

        if flag_strds and map_names:
            gs.message("Registering {} maps in strds '{}'...".format(
                len(map_names), output))
            register_strds(output, map_names, date_strings, 'monthly')
            gs.message("strds '{}' created.".format(output))

    gs.message("Done. {} map(s) imported.".format(len(map_names)))


if __name__ == '__main__':
    main()
