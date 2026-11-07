"""Run with: python -m unittest test_pdf_report -v

Uses the included DXFs and temporary folders; does not open the GUI or change settings.
"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import TANNest as app


class PdfReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.st = app.Settings(material_cost_lb=1, weld_cost_ft=12, remnant_credit=25)
        parts = []
        for path in sorted((Path(__file__).parent / 'DXF').glob('*.dxf')):
            loaded, _ = app.load_dxf(str(path), cls.st)
            parts.extend(loaded)
        if not parts:
            raise RuntimeError('Place the included DXF folder beside this test file.')
        cls.result = app.optimize(parts, app.DEFAULT_SHEETS, cls.st)

    def test_complete_export_preserves_result(self):
        res = self.result
        before = copy.deepcopy((res.cost, res.seams))
        geometry = [app.placed_geometry(b, self.st) for b in res.bins]
        with tempfile.TemporaryDirectory() as folder:
            files = app.write_outputs(res, self.st, folder)
            self.assertTrue(all(Path(f).is_file() for f in files))
            self.assertEqual(Path(folder, 'Nest_Report.txt').read_text(encoding='utf-8'),
                             app.report_text(res, self.st))
            self.assertTrue(Path(folder, 'Nest_Report.pdf').read_bytes().startswith(b'%PDF-'))
        self.assertEqual(before, (res.cost, res.seams))
        self.assertEqual(geometry, [app.placed_geometry(b, self.st) for b in res.bins])

    def test_failed_pdf_build_preserves_previous_report(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder, 'Nest_Report.pdf')
            target.write_bytes(b'previous report')
            with patch('reportlab.platypus.SimpleDocTemplate.build', side_effect=RuntimeError('test failure')):
                with self.assertRaises(RuntimeError):
                    app.write_pdf_report(self.result, self.st, str(target))
            self.assertEqual(target.read_bytes(), b'previous report')
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_unpriced_long_names_and_quantity(self):
        st = app.Settings(effort=20)
        parts, _ = app.load_dxf(str(Path(__file__).parent/'DXF/BackWall.dxf'), st)
        parts[0].name = 'Wall & <profile> ' + 'long name ' * 15
        parts[0].qty = 3
        res = app.optimize(parts, [(200, 600)], st)
        self.assertFalse(res.model.priced)
        self.assertEqual(res.n_seams, 0)
        with tempfile.TemporaryDirectory() as folder:
            app.write_pdf_report(res, st, str(Path(folder, 'stress.pdf')))

    def test_metric_and_rotated_original_coordinates(self):
        # Tall input forces normalization rotation and original-Y seam labels.
        import ezdxf
        st = app.Settings(border=10, spacing=25, min_seg=150, seam_clear=50,
                          seam_step=1, effort=20, material_cost_lb=1)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, 'metric.dxf')
            doc = ezdxf.new()
            doc.units = 4
            doc.modelspace().add_lwpolyline([(100, 200), (600, 200),
                                           (600, 5200), (100, 5200)], close=True)
            doc.saveas(source)
            parts, _ = app.load_dxf(str(source), st)
            parts[0].qty = 2
            res = app.optimize(parts, [(1000, 2000)], st)
            self.assertTrue(res.preps[0].rotated0)
            self.assertTrue(res.seams['metric'])
            self.assertTrue(all(app.seam_label(res.preps[0], s).startswith('SEAM Y=')
                                for s in res.seams['metric']))
            app.write_pdf_report(res, st, str(Path(folder, 'metric.pdf')))


if __name__ == '__main__':
    unittest.main()
