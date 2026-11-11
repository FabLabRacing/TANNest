# TANNest

TANNest is a sheet nesting and part-splitting tool for the plasma table. You give it your stock sheet sizes and DXF parts. It figures out which sheets to buy and where to put the seams on parts too long for any sheet. It writes one cut-ready DXF per sheet, plus seam maps and a cost report.

Layouts are scored in dollars: plate cost, minus credit for usable remnants, plus weld cost for every seam. That way the app can tell when a few extra seams are worth the steel they save, and when they aren't.

> **All output is a draft.** Check every sheet in SheetCam (layers, cut direction, lead-ins, kerf) before cutting. Have a qualified TANN engineer review seam locations on any structural or pressure-bearing part.

---

## Contents

1. [Installation](#installation)
2. [Quick start](#quick-start)
3. [How TANNest reads a DXF](#how-tannest-reads-a-dxf)
4. [Part options](#part-options)
5. [Settings reference](#settings-reference)
6. [Cost settings](#cost-settings)
7. [Output files](#output-files)
8. [Setting up SheetCam](#setting-up-sheetcam)
9. [Reading the report](#reading-the-report)
10. [Limitations](#limitations)
11. [Troubleshooting](#troubleshooting)

---

## Installation

TANNest is a single Python file. It needs Python 3.10 or newer and three libraries: **ezdxf** (DXF reading/writing), **shapely** (geometry) and **reportlab** (PDF report). The app opens without reportlab, but export stops with a message until it is installed.

### Windows

1. Download Python from [python.org/downloads](https://www.python.org/downloads/).
2. Run the installer. On the first screen, check **"Add python.exe to PATH"**. Leave **"tcl/tk and IDLE"** checked; that's the tkinter GUI library, and it's on by default.
3. Open **Command Prompt** and install the libraries:

   ```
   pip install ezdxf shapely reportlab
   ```

4. Copy `tannest.py` to a folder of your choice, for example `C:\Tools\TANNest\`.
5. Run it:

   ```
   python C:\Tools\TANNest\tannest.py
   ```

**Desktop shortcut without the console window:** rename the file to `tannest.pyw`, or make a shortcut with this target:

```
pythonw.exe "C:\Tools\TANNest\tannest.py"
```

`pythonw` runs the app without the black command window.

### Linux

```
sudo apt install python3-tk
pip install ezdxf shapely reportlab   # add --break-system-packages on newer Ubuntu, or use a venv
python3 tannest.py
```

### Checking the install

If the window opens and shows the default sheet list, you're set. If you get `ModuleNotFoundError`, the module name in the error tells you what's missing:

| Missing module | Fix |
|---|---|
| `ezdxf`, `shapely` or `reportlab` | Rerun `pip install ezdxf shapely reportlab` |
| `tkinter` (Windows) | Rerun the Python installer, choose Modify, and enable tcl/tk |
| `tkinter` (Linux) | `sudo apt install python3-tk` |

---

## Quick start

1. **Check the sheet list** (top left). It starts with the nine standard sizes: 48, 60 and 72 wide by 96, 120 and 144 long.
   - Double-click a sheet, or select it and press **On/Off**, to include or exclude it.
   - Type a width and length and press **Add** for a new size.
   - **Defaults** restores the standard list.
2. **Set the cost panel**: material, thickness, $/lb, and weld $/ft. If you leave the prices at 0, layouts are scored by total sheet area instead.
3. **Add DXF...** and pick one or more part files. The Report / Log tab shows what was found in each file: units, contours, holes, duplicates removed, and anything ignored.
4. **Set quantities and options**: double-click a part to open its options. Use **Duplicate** when you need the same DXF twice with different options (see [Part options](#part-options)).
5. **Optimize**. A typical job takes a second or two. The Preview tab shows every sheet:
   - black lines are outside cuts
   - red lines are shared cut lines
   - green lines are holes
6. **Check the Report / Log tab** for cost, seam locations, and the trade-off table.
7. **Export DXFs + PDFs...** and pick an output folder.

---

## How TANNest reads a DXF

TANNest reads LINE, ARC, CIRCLE, LWPOLYLINE, POLYLINE, SPLINE and ELLIPSE entities, and explodes block INSERTs. Loose lines and arcs are chained end to end into closed contours. Text, dimensions, hatches and points are ignored; the log lists what was skipped.

Each closed contour gets a role based on what it sits inside:

| Contour | Treated as | Output layer |
|---|---|---|
| Outermost outline | The part | `OUTSIDE` |
| Inside loop with other loops inside it | A drop part that shares its cut line with the opening (cover plate, hatch, disk) | `COMMON_LINE` |
| Inside loop with nothing inside it | A hole | `HOLES` |

For example, on the test wall the Ø32" circle has a Ø12.25" bore inside it, so the circle is a shared-cutline disk. The 26.5" square has bolt holes inside it, so it's a shared-cutline plate. The bore and bolt holes are holes.

**A plain opening with nothing inside it** (no bore, no holes) is treated as a hole, and its drop is scrap. If that drop is a part you want to keep, add any small feature inside it in CAD, or handle that contour manually in SheetCam.

**Several separate outlines in one DXF** become separate parts, named `file#1`, `file#2`, and so on, largest first.

**Duplicate contours** (the same shape drawn twice) are removed automatically so nothing gets cut twice.

---

## Part options

Double-click a part (or select it and press **Edit...**):

| Option | What it does |
|---|---|
| **Name** | Used in file names and the report. Must be unique. |
| **Quantity** | Number of copies. Set to 0 to keep a part in the list but leave it out of the run. |
| **Outside profile only** | Drops all internal features and cuts just the outline. |
| **Allow 90 deg rotation** | Lets the packer turn the part sideways. It also lets a tall-and-narrow part be treated with its long side as the length. |
| **Allow splitting into segments** | Lets an oversize part be cut into weldable segments. If this is off and the part doesn't fit any sheet, the run stops with an error. |

**Same DXF, two versions** (for example, one wall with features and one without): add the DXF, select it, press **Duplicate**, then edit the copy. Give it a new name and check *Outside profile only*.

---

## Settings reference

All lengths are in drawing units, which is normally inches.

| Setting | Default | Meaning |
|---|---|---|
| Edge border | 0.375 | Minimum distance from any part to the sheet edge |
| Part spacing | 1.0 | Minimum distance between parts on the same sheet |
| Seam clearance | 2.0 | Minimum distance from a seam to any internal feature or notch |
| Corner clearance | 2.0 | Minimum distance from a seam to any inside (concave) corner of the outline. Set this from your weld practice; it is checked separately from feature clearance. |
| Min segment length | 6.0 | Shortest piece a split is allowed to produce |
| Seam rounding step | 0.0625 | Seams land on multiples of this (1/16"). Use 0 for exact positions. |
| Arc flatten tol. | 0.005 | Chord tolerance when an arc has to become a polyline (only on split segment outlines) |
| Endpoint join tol. | 0.005 | How close two line ends must be to count as connected. Raise it slightly for sloppy drawings. |
| Search effort | 400 | Base search effort. It is scaled up automatically with the number of parts and seam options, so big jobs get a more thorough search. Raise it if results change noticeably between runs. |
| Time limit (s) | 60 | The search stops at this limit whatever the effort; the report notes it if that happened. |
| Keep seams out of notches | on | Seams stay out of notches: concave cut-outs shorter than 25% of the part's length. Longer concave areas (a narrowed middle section, long side reliefs) are treated as part of the shape, so only their corners are kept clear. If no layout can avoid a notch, seams may cross it but inside corners are still kept clear; only as a last resort are corners given up, with a warning. |

Settings and the sheet list are saved when you close the app. The file is `.tannest.json` in your user folder.

---

## Cost settings

| Setting | Meaning |
|---|---|
| Material | Picks a density: steel 0.2836, 304 stainless 0.289, 6061 aluminum 0.0975 lb/in³. Choose Custom and type your own. |
| Thickness (in) | Plate thickness in inches, even if the DXF is in mm |
| Density (lb/in³) | Filled in by Material, or entered by hand |
| Material $/lb | What you pay for plate |
| Weld $/ft | Cost of one foot of **finished** seam: fit-up, prep, all passes, both sides if welded both sides, and grinding |
| Remnant credit % | Share of a remnant's material value to credit back. Leave at 0 if drops go to scrap. |
| Min remnant (in) | A strip only counts as a usable remnant if its narrow side is at least this wide |

**How a layout is scored:**

```
plate $  = full area of every sheet used × thickness × density × $/lb
weld $   = total seam length (ft) × weld $/ft
credit   = usable remnant area × thickness × density × $/lb × credit %
total    = plate $ − credit + weld $
```

The layout with the lowest total wins. Ties go to less sheet area, then fewer seams, then fewer sheets. With both prices at 0, the score is simply total sheet area.

**Rule of thumb:** extra seams pay off when a foot of finished seam costs less than the plate it saves. The report works out that break-even weight per foot for your actual job.

---

## Output files

**Export DXFs + PDFs...** writes these files to the folder you choose:

| File | Contents |
|---|---|
| `Sheet01_72x144_WallA-S1.dxf`, ... | One file per sheet. Sheet origin is at the lower left (0,0); length runs along X. |
| `SeamMap_<part>.dxf` | Each split part in its original coordinates, with seam lines, seam labels, and each piece labeled with the sheet it comes from (all on `SEAM_REF`) |
| `Nest_Report.txt` | The same report shown in the Report / Log tab |
| `Assembly_Map.pdf` | For the shop floor: each split part as drawn in its DXF, with numbered seams and every piece tagged with its name and sheet number, plus a piece table (sheet, cut file, size) and a seam table (location, weld length, which pieces it joins). One page per copy of each split part, then a list of parts cut whole and their sheets. |
| `Nest_Report.pdf` | Formatted report: summary, cost and material balance, stock to purchase, job inputs, settings, alternatives and break-even table, one page per sheet with a numbered layout drawing and remnant credits, and one page per part with its seams and where each piece was cut |

**Layers in the sheet files:**

| Layer | Color | Use |
|---|---|---|
| `OUTSIDE` | white/black | Part outline. Outside offset. |
| `COMMON_LINE` | red | Shared cut line between an opening and its drop part. On-line, no offset. |
| `HOLES` | green | Holes. Inside offset. |
| `SHEET_REF_NOCUT` | gray | Sheet outline for reference. **Do not assign an operation.** |

**Segment names** read `PartName-S1`, `-S2`, ..., numbered along the part's long axis. With quantity above 1, the instance number comes first: `EndWall_2-S1`.

Un-split parts and all internal features keep their true lines, arcs and circles. Split segment outlines are written as polylines, so arcs on a cut edge are flattened to the tolerance setting.

---

## Setting up SheetCam

Set up operations by **layer** rather than letting SheetCam decide inside/outside on its own. A drop part with holes in it puts those holes three levels deep, and the automatic detection can misjudge them.

Suggested operations, in cut order:

1. **HOLES**: plasma cut, inside offset
2. **COMMON_LINE**: plasma cut, **no offset (on-line)**
3. **OUTSIDE**: plasma cut, outside offset

Leave `SHEET_REF_NOCUT` with no operation (hide it if you like).

**Kerf on shared cut lines:** an on-line cut splits the kerf, so both the opening and the drop part lose about half a kerf on that edge. A drop plate comes out roughly one kerf undersize overall. If the drop's size matters more than the opening's, put that contour on an inside offset instead.

**Seam edges** are normal outside cuts with kerf compensation, so each segment holds its drawn dimension. TANNest does not add root gaps or bevel prep; build those into the drawing or the weld procedure.

Saving a SheetCam job template with these three layer operations makes every TANNest sheet a quick load.

---

## Reading the report

- **Cost summary**:
  - plate bought (lb and $)
  - net part weight and its share of the plate
  - credited remnants
  - scrap/skeleton weight
  - weld feet and $
  - total
- **Trade-off table**: the best layout found at each seam count, priced at your settings, with the chosen one marked. Use it to see what fewer or more seams would have cost.
- **Best when weld costs**: a column on the trade-off table giving the weld $/ft range over which each layout would be the cheapest (plate minus remnant credit plus weld). "Never best" means another layout always beats it. Totals in the table are at your entered weld rate; the ranges show how the choice would change at other rates, comparing only the layouts listed.
- **Per sheet**:
  - size, weight and cost
  - remnant length past the last part
  - every part's size, position, and whether it was rotated
- **Seams**:
  - seam positions in original drawing coordinates, which match the SeamMap DXF
  - segment lengths
  - weld length of each seam

---

## Limitations

- **Rectangular nesting.** Parts are packed by bounding box at 0 or 90 degrees. There is no true-shape nesting, and small parts are never placed inside openings or between irregular edges. Irregular parts (triangles, rings, L-shapes) waste more than a true-shape nester would.
- **One split direction.** Seams are perpendicular to a part's long axis only. A part too wide for every sheet in both directions can't be split and stops the run.
- **Straight seams only.** No stepped or puzzle joints.
- **Heuristic search.** The search is fast and usually very good, but it can't prove it found the absolute best layout. If a result looks off, raise Search effort or compare against the trade-off table.
- **Flat costs.** Each sheet size has the same $/lb, and weld cost is the same per foot regardless of position or orientation.
- **One material per run.** Thickness and density apply to every part in the job, so run different plate thicknesses as separate jobs.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Log says *open contour(s) ignored* | Endpoints don't meet. Fix the drawing, or raise Endpoint join tol. a little (0.01 to 0.02). |
| Part shows the wrong size | Stray geometry in the file (title block, dimension lines drawn as lines). Clean the DXF down to cut geometry. |
| *too wide for every enabled sheet* | The part's short side doesn't fit any enabled sheet minus the borders. Enable a wider sheet or check the drawing. |
| *could not place seams clear of internal features* | Features are packed too closely for the seam clearance. Lower Seam clearance or Min segment length, or enable longer sheets. |
| *no seam layout avoids the notches* | The run still completed; a seam may cross a notch, but inside corners are still kept at Corner clearance. Check the SeamMap DXF. |
| *WARNING no seam layout keeps clear of inside corners* | Last-resort fallback: a seam may sit close to an inside corner. Check the SeamMap DXF, then lower Corner clearance deliberately or review the part with engineering. |
| *search stopped at the time limit* | Large job; raise Time limit (s) for a more thorough search. |
| Costs are all $0.00 | Prices aren't set, so the layout was scored by sheet area. Enter Material $/lb and Weld $/ft. |
| Mixed units warning | Parts were drawn in different units. Redraw or convert so every part uses the same units. |
| Window too small or settings cut off | Enlarge the window; the minimum size is 1000 x 650. |

---

*TANNest is an internal tool for TANN Corporation. Output is a draft for review before cutting.*
