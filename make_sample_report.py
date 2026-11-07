"""Generate the demonstration PDF using the included wall drawings."""
from pathlib import Path
import TANNest as app


def main():
    root = Path(__file__).resolve().parent
    settings = app.Settings(material_cost_lb=1.0, weld_cost_ft=12.0, remnant_credit=25)
    parts = []
    for source in sorted((root / 'DXF').glob('*.dxf')):
        loaded, info = app.load_dxf(str(source), settings)
        print(info)
        parts.extend(loaded)
    result = app.optimize(parts, app.DEFAULT_SHEETS, settings)
    target = root / 'Nest_Report_Sample.pdf'
    app.write_pdf_report(result, settings, str(target),
        'SAMPLE - supplied wall DXFs; one copy each. Illustrative prices: '
        '$1.00/lb plate, $12.00/ft welding, 25% remnant credit. '
        'Default geometry settings and 0.25-inch plate. Not a shop quote.')
    print(f'Created {target}')


if __name__ == '__main__':
    main()
