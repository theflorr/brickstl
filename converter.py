import hashlib
import os
import re
import sqlite3
import struct
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from openpyxl import Workbook

LDU_TO_MM = 0.4
SEARCH_PREFIXES = ["parts/", "p/"]
PARTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parts")
_PARTS_INDEX = None
_PARTS_INDEX_LOCK = threading.Lock()
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database")
DB_PARTS = [os.path.join(DB_DIR, f"rebrickable.db.{i:03d}") for i in (1, 2, 3)]
DB_SIZE = 57548800
DB_SHA256 = "76f0b19a8bd4a46335e03bfa25144543741af9767ecb1ab066168c6c44b81d75"
_RESOLVED_DB = None
_DB_JOIN_LOCK = threading.Lock()


def resolve_db_path():
    global _RESOLVED_DB
    if _RESOLVED_DB is not None:
        return _RESOLVED_DB
    with _DB_JOIN_LOCK:
        if _RESOLVED_DB is not None:
            return _RESOLVED_DB
        for part in DB_PARTS:
            if not os.path.exists(part):
                raise ConversionError(
                    "parts database not found, expected database/rebrickable.db "
                    "or the split chunks database/rebrickable.db.001/.002/.003"
                )
        out_dir = os.path.join(tempfile.gettempdir(), "brickstl_db")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "rebrickable.db")
        if not (os.path.exists(out_path) and os.path.getsize(out_path) == DB_SIZE):
            tmp_path = out_path + f".tmp-{os.getpid()}"
            digest = hashlib.sha256()
            with open(tmp_path, "wb") as out:
                for part in DB_PARTS:
                    with open(part, "rb") as f:
                        while True:
                            buf = f.read(1024 * 1024)
                            if not buf:
                                break
                            digest.update(buf)
                            out.write(buf)
            if digest.hexdigest() != DB_SHA256:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise ConversionError("database chunks are corrupt (hash mismatch)")
            os.replace(tmp_path, out_path)
        _RESOLVED_DB = out_path
        return _RESOLVED_DB

BED_MARGIN = 5.0
PLATE_LIMIT = 40

COMMON_PRINTERS = {
    "ender3": (220, 220),
    "ender3v2": (220, 220),
    "prusa_mk3": (250, 210),
    "prusa_mk4": (250, 220),
    "bambu_a1": (256, 256),
    "bambu_a1_mini": (180, 180),
    "bambu_x1c": (256, 256),
    "bambu_p1s": (256, 256),
    "ender5": (220, 220),
    "ender5plus": (350, 350),
    "creality_k1": (220, 220),
    "elegoo_neptune4": (225, 225),
}


IDENTITY = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)


def is_ldraw_text(text):
    stripped = text.strip()
    if not stripped:
        return False
    head = stripped[:400].lower()
    if "<html" in head or "<!doctype" in head:
        return False
    return stripped[0] in "012345"


GEOMETRY_WORKERS = 32


def download_candidates(name):
    yield name
    m = re.match(r"^(.*)pr[0-9]+\.dat$", name)
    if m:
        yield m.group(1) + ".dat"


def _parts_index():
    global _PARTS_INDEX
    if _PARTS_INDEX is None:
        with _PARTS_INDEX_LOCK:
            if _PARTS_INDEX is None:
                index = {}
                if os.path.isdir(PARTS_DIR):
                    for dirpath, _dirnames, filenames in os.walk(PARTS_DIR):
                        for fn in filenames:
                            if not fn.lower().endswith(".dat"):
                                continue
                            full = os.path.join(dirpath, fn)
                            rel = os.path.relpath(full, PARTS_DIR).replace(os.sep, "/").lower()
                            index.setdefault(rel, full)
                _PARTS_INDEX = index
    return _PARTS_INDEX


def read_local_ldraw(key):
    path = _parts_index().get(key.lower())
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    if is_ldraw_text(text):
        return text
    return None


def fetch_ldraw_file(name):
    name = name.replace("\\", "/").lower()
    for candidate in download_candidates(name):
        for prefix in SEARCH_PREFIXES:
            text = read_local_ldraw(prefix + candidate)
            if text is not None:
                return text
    return None


def convert_one_part(args):
    part_num, color_id, qty, colors, tolerance_mm = args
    triangles = part_to_triangles(part_num)
    if not triangles:
        return (part_num, color_id, qty, None)
    triangles = repair_mesh(triangles)
    if tolerance_mm:
        triangles = apply_printer_tolerance(triangles, tolerance_mm)
    color_info = colors.get(int(color_id), {"name": f"Color{color_id}", "hex": "808080"})
    return (part_num, color_id, qty, (triangles, color_info))


DEFAULT_TOLERANCE_MM = 0.15
RING_ANGLE_TOL_DEG = 8.0
RING_RADIUS_TOL_MM = 0.03
MIN_RING_POINTS = 8
MIN_RING_RADIUS_MM = 0.8
MAX_RING_RADIUS_MM = 8.0


def _round_key(v, places):
    return round(v, places)


def _group_by_axis_and_height(triangles):
    buckets = {0: {}, 1: {}, 2: {}}
    for tri in triangles:
        for p in tri:
            for axis in (0, 1, 2):
                h = _round_key(p[axis], 2)
                buckets[axis].setdefault(h, set()).add(p)
    return buckets


def _spatial_clusters(pts2d, gap_factor=2.5):
    import math
    n = len(pts2d)
    if n <= 1:
        return [pts2d]
    dists = []
    for i in range(min(n, 60)):
        ax, ay, _ = pts2d[i]
        best = None
        for j in range(n):
            if i == j:
                continue
            bx, by, _ = pts2d[j]
            d = (ax - bx) ** 2 + (ay - by) ** 2
            if best is None or d < best:
                best = d
        if best is not None:
            dists.append(best ** 0.5)
    if not dists:
        return [pts2d]
    dists.sort()
    typical_spacing = dists[len(dists) // 2]
    threshold = typical_spacing * gap_factor

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        ax, ay, _ = pts2d[i]
        for j in range(i + 1, n):
            bx, by, _ = pts2d[j]
            if (ax - bx) ** 2 + (ay - by) ** 2 <= threshold ** 2:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(pts2d[i])
    return list(groups.values())


def _fit_rings(points, axis):
    import math
    other = [0, 1, 2]
    other.remove(axis)
    a_idx, b_idx = other
    pts2d = [(p[a_idx], p[b_idx], p) for p in points]
    if len(pts2d) < MIN_RING_POINTS:
        return []

    rings = []
    for cluster in _spatial_clusters(pts2d):
        if len(cluster) < MIN_RING_POINTS:
            continue

        a_vals = [p[0] for p in cluster]
        b_vals = [p[1] for p in cluster]
        ca = (min(a_vals) + max(a_vals)) / 2.0
        cb = (min(b_vals) + max(b_vals)) / 2.0

        radii = [((a - ca) ** 2 + (b - cb) ** 2) ** 0.5 for a, b, _ in cluster]
        r_med = sorted(radii)[len(radii) // 2]
        if r_med < MIN_RING_RADIUS_MM or r_med > MAX_RING_RADIUS_MM:
            continue

        members = [
            (a, b, orig) for (a, b, orig), r in zip(cluster, radii)
            if abs(r - r_med) <= RING_RADIUS_TOL_MM
        ]
        if len(members) < MIN_RING_POINTS:
            continue

        angles = sorted(math.atan2(b - cb, a - ca) for a, b, _ in members)
        gaps = [angles[i + 1] - angles[i] for i in range(len(angles) - 1)]
        gaps.append(angles[0] + 2 * math.pi - angles[-1])
        if max(gaps) > math.radians(360 - RING_ANGLE_TOL_DEG * len(members)):
            continue

        rings.append((ca, cb, r_med, [orig for _, _, orig in members]))

    return rings


def _classify_ring(ring, axis, tri_by_vertex):
    ca, cb, r, members = ring
    other = [0, 1, 2]
    other.remove(axis)
    a_idx, b_idx = other
    member_set = set(members)

    outward = inward = 0
    for tri in tri_by_vertex:
        v1, v2, v3 = tri
        if not (v1 in member_set or v2 in member_set or v3 in member_set):
            continue
        n = normal(v1, v2, v3)
        if abs(n[axis]) > 0.5:
            continue
        cx = (v1[a_idx] + v2[a_idx] + v3[a_idx]) / 3.0 - ca
        cy = (v1[b_idx] + v2[b_idx] + v3[b_idx]) / 3.0 - cb
        rad_len = (cx ** 2 + cy ** 2) ** 0.5 or 1.0
        radial = (cx / rad_len, cy / rad_len)
        dot = n[a_idx] * radial[0] + n[b_idx] * radial[1]
        if dot > 0.3:
            outward += 1
        elif dot < -0.3:
            inward += 1

    if outward == 0 and inward == 0:
        return None
    return "male" if outward >= inward else "female"


def apply_printer_tolerance(triangles, tolerance_mm):
    if tolerance_mm <= 0:
        return triangles

    buckets = _group_by_axis_and_height(triangles)
    offsets = {}

    for axis, heights in buckets.items():
        for _height, points in heights.items():
            if len(points) < MIN_RING_POINTS:
                continue
            for ring in _fit_rings(points, axis):
                classification = _classify_ring(ring, axis, triangles)
                if classification is None:
                    continue
                ca, cb, r, members = ring
                other = [0, 1, 2]
                other.remove(axis)
                a_idx, b_idx = other
                delta = -tolerance_mm if classification == "male" else tolerance_mm
                for p in members:
                    a, b = p[a_idx], p[b_idx]
                    dx, dy = a - ca, b - cb
                    dist = (dx ** 2 + dy ** 2) ** 0.5
                    if dist < 1e-6:
                        continue
                    scale = (r + delta) / r
                    new_a = ca + dx * scale
                    new_b = cb + dy * scale
                    key = p
                    da = new_a - a
                    db = new_b - b
                    prev = offsets.get(key)
                    if prev is None or (da ** 2 + db ** 2) > (prev[0] ** 2 + prev[1] ** 2):
                        offsets[key] = (da, db, axis, a_idx, b_idx)

    if not offsets:
        return triangles

    def adjust(p):
        entry = offsets.get(p)
        if entry is None:
            return p
        da, db, axis, a_idx, b_idx = entry
        coords = list(p)
        coords[a_idx] += da
        coords[b_idx] += db
        return tuple(coords)

    return [tuple(adjust(v) for v in tri) for tri in triangles]


def load_colors():
    db_path = resolve_db_path()
    con = sqlite3.connect(db_path)
    try:
        colors = {}
        for color_id, name, rgb in con.execute("SELECT id, name, rgb FROM colors"):
            colors[color_id] = {"name": name, "hex": rgb}
        return colors
    finally:
        con.close()


def mat_mult(m, v):
    x, y, z = v
    a, b, c, d, e, f, g, h, i, tx, ty, tz = m
    return (
        a * x + b * y + c * z + tx,
        d * x + e * y + f * z + ty,
        g * x + h * y + i * z + tz,
    )


def combine(parent, child):
    pa, pb, pc, pd, pe, pf, pg, ph, pi, ptx, pty, ptz = parent
    ca, cb, cc, cd, ce, cf, cg, ch, ci, ctx, cty, ctz = child
    r1 = (pa, pb, pc, pd, pe, pf, pg, ph, pi)
    r2 = (ca, cb, cc, cd, ce, cf, cg, ch, ci)

    def rmul(m1, m2):
        a1, b1, c1, d1, e1, f1, g1, h1, i1 = m1
        a2, b2, c2, d2, e2, f2, g2, h2, i2 = m2
        return (
            a1 * a2 + b1 * d2 + c1 * g2,
            a1 * b2 + b1 * e2 + c1 * h2,
            a1 * c2 + b1 * f2 + c1 * i2,
            d1 * a2 + e1 * d2 + f1 * g2,
            d1 * b2 + e1 * e2 + f1 * h2,
            d1 * c2 + e1 * f2 + f1 * i2,
            g1 * a2 + h1 * d2 + i1 * g2,
            g1 * b2 + h1 * e2 + i1 * h2,
            g1 * c2 + h1 * f2 + i1 * i2,
        )

    nr = rmul(r1, r2)
    ntx, nty, ntz = mat_mult(parent, (ctx, cty, ctz))
    return (nr[0], nr[1], nr[2], nr[3], nr[4], nr[5], nr[6], nr[7], nr[8], ntx, nty, ntz)


def parse_part(name, transform, triangles, color_code, depth=0):
    if depth > 15:
        return
    text = fetch_ldraw_file(name)
    if text is None:
        print(f"warning: could not fetch subpart {name}")
        return

    for line in text.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        ltype = parts[0]

        if ltype == "1" and len(parts) >= 14:
            nums = list(map(float, parts[2:14]))
            x, y, z = nums[0], nums[1], nums[2]
            a, b, c, d, e, f, g, h, i = nums[3:12]
            sub_transform = (a, b, c, d, e, f, g, h, i, x, y, z)
            combined = combine(transform, sub_transform)
            sub_name = " ".join(parts[14:])
            sub_color = parts[1]
            next_color = color_code if sub_color == "16" else sub_color
            parse_part(sub_name, combined, triangles, next_color, depth + 1)

        elif ltype == "3" and len(parts) >= 11:
            nums = list(map(float, parts[2:11]))
            v1 = mat_mult(transform, (nums[0], nums[1], nums[2]))
            v2 = mat_mult(transform, (nums[3], nums[4], nums[5]))
            v3 = mat_mult(transform, (nums[6], nums[7], nums[8]))
            triangles.append((v1, v2, v3))

        elif ltype == "4" and len(parts) >= 14:
            nums = list(map(float, parts[2:14]))
            v1 = mat_mult(transform, (nums[0], nums[1], nums[2]))
            v2 = mat_mult(transform, (nums[3], nums[4], nums[5]))
            v3 = mat_mult(transform, (nums[6], nums[7], nums[8]))
            v4 = mat_mult(transform, (nums[9], nums[10], nums[11]))
            triangles.append((v1, v2, v3))
            triangles.append((v1, v3, v4))


def part_to_triangles(part_number):
    part_name = part_number.lower()
    if not part_name.endswith(".dat"):
        part_name += ".dat"
    triangles = []
    parse_part(part_name, IDENTITY, triangles, "16")
    return [
        (
            (v1[0] * LDU_TO_MM, v1[1] * LDU_TO_MM, v1[2] * LDU_TO_MM),
            (v2[0] * LDU_TO_MM, v2[1] * LDU_TO_MM, v2[2] * LDU_TO_MM),
            (v3[0] * LDU_TO_MM, v3[1] * LDU_TO_MM, v3[2] * LDU_TO_MM),
        )
        for v1, v2, v3 in triangles
    ]


def repair_mesh(triangles):
    cleaned = []
    seen = set()
    for v1, v2, v3 in triangles:
        key = tuple(sorted((v1, v2, v3)))
        if key in seen:
            continue
        ux, uy, uz = v2[0] - v1[0], v2[1] - v1[1], v2[2] - v1[2]
        vx, vy, vz = v3[0] - v1[0], v3[1] - v1[1], v3[2] - v1[2]
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        if nx == 0 and ny == 0 and nz == 0:
            continue
        seen.add(key)
        cleaned.append((v1, v2, v3))
    return cleaned


def normal(v1, v2, v3):
    ux, uy, uz = v2[0] - v1[0], v2[1] - v1[1], v2[2] - v1[2]
    vx, vy, vz = v3[0] - v1[0], v3[1] - v1[1], v3[2] - v1[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = (nx ** 2 + ny ** 2 + nz ** 2) ** 0.5
    if length == 0:
        return (0.0, 0.0, 0.0)
    return (nx / length, ny / length, nz / length)


def bounding_box(triangles):
    xs = [v[0] for tri in triangles for v in tri]
    ys = [v[1] for tri in triangles for v in tri]
    zs = [v[2] for tri in triangles for v in tri]
    return (min(xs), max(xs)), (min(ys), max(ys)), (min(zs), max(zs))


def triangle_area(v1, v2, v3):
    ux, uy, uz = v2[0] - v1[0], v2[1] - v1[1], v2[2] - v1[2]
    vx, vy, vz = v3[0] - v1[0], v3[1] - v1[1], v3[2] - v1[2]
    cx, cy, cz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    return 0.5 * (cx * cx + cy * cy + cz * cz) ** 0.5


def _proper_axis_rotations():
    import itertools
    rotations = []
    for perm in itertools.permutations(range(3)):
        inversions = sum(perm[i] > perm[j] for i in range(3) for j in range(i + 1, 3))
        permutation_sign = -1 if inversions % 2 else 1
        for signs in itertools.product((-1, 1), repeat=3):
            if permutation_sign * signs[0] * signs[1] * signs[2] == 1:
                rotations.append((perm, signs))
    return rotations


AXIS_ROTATIONS = _proper_axis_rotations()


def rotate_triangles(triangles, rotation):
    perm, signs = rotation
    return [tuple(tuple(signs[i] * point[perm[i]] for i in range(3)) for point in tri) for tri in triangles]


def smart_orient_triangles(triangles):
    best, best_score = triangles, None
    for rotation in AXIS_ROTATIONS:
        rotated = rotate_triangles(triangles, rotation)
        (minx, maxx), (miny, maxy), (minz, maxz) = bounding_box(rotated)
        height = maxz - minz
        footprint = max((maxx - minx) * (maxy - miny), 0.01)
        tolerance = max(0.05, height * 0.002)
        contact_area = unsupported_area = steep_area = 0.0
        for v1, v2, v3 in rotated:
            area = triangle_area(v1, v2, v3)
            _, _, nz = normal(v1, v2, v3)
            if max(v1[2], v2[2], v3[2]) <= minz + tolerance:
                contact_area += area * abs(nz)
            elif nz < -0.15:
                unsupported_area += area * min(1.0, -nz)
                if nz < -0.7:
                    steep_area += area * (-nz)
        slenderness = height / max(footprint ** 0.5, 0.1)
        score = (unsupported_area * 8.0 + steep_area * 5.0 + height * 0.35
                 + slenderness * 3.0 - contact_area * 1.5)
        tie = (score, height, -contact_area, footprint)
        if best_score is None or tie < best_score:
            best, best_score = rotated, tie
    return best


def write_stl(triangles, out_path, name="part"):
    with open(out_path, "wb") as f:
        header = f"brickstl {name}".encode("utf-8")[:80]
        f.write(header.ljust(80, b"\0"))
        f.write(struct.pack("<I", len(triangles)))
        for v1, v2, v3 in triangles:
            n = normal(v1, v2, v3)
            f.write(struct.pack("<3f", *n))
            f.write(struct.pack("<3f", *v1))
            f.write(struct.pack("<3f", *v2))
            f.write(struct.pack("<3f", *v3))
            f.write(struct.pack("<H", 0))


def write_plate_stl(plate, out_path, name="plate"):
    all_triangles = [tri for triangles, _ in plate for tri in triangles]
    write_stl(all_triangles, out_path, name)


class ConversionError(Exception):
    pass


def get_set_parts(set_num):
    set_num = set_num.strip()
    if not set_num.endswith("-1") and "-" not in set_num:
        set_num += "-1"
    db_path = resolve_db_path()
    con = sqlite3.connect(db_path)
    try:
        top = con.execute(
            "SELECT id FROM inventories WHERE set_num=? ORDER BY version DESC LIMIT 1",
            (set_num,),
        ).fetchone()
        if top is None:
            raise ConversionError(f"set '{set_num}' was not found in the local database")
        parts = {}
        seen = set()
        stack = [(top[0], 1)]
        while stack:
            inv_id, mult = stack.pop()
            if inv_id in seen:
                continue
            seen.add(inv_id)
            for part_num, color_id, qty, is_spare in con.execute(
                "SELECT part_num, color_id, quantity, is_spare FROM inventory_parts WHERE inventory_id=?",
                (inv_id,),
            ):
                if is_spare:
                    continue
                key = (part_num, str(color_id))
                parts[key] = parts.get(key, 0) + qty * mult
            for child_num, child_qty in con.execute(
                "SELECT set_num, quantity FROM inventory_sets WHERE inventory_id=?",
                (inv_id,),
            ):
                child = con.execute(
                    "SELECT id FROM inventories WHERE set_num=? ORDER BY version DESC LIMIT 1",
                    (child_num,),
                ).fetchone()
                if child is not None:
                    stack.append((child[0], mult * child_qty))
    finally:
        con.close()
    if not parts:
        raise ConversionError(f"set '{set_num}' has no parts")
    return parts


def write_3mf(objects, out_path, name="model"):
    color_order = []
    color_index = {}
    for _triangles, color_hex in objects:
        hexval = (color_hex or "808080").lstrip("#").upper()
        if hexval not in color_index:
            color_index[hexval] = len(color_order)
            color_order.append(hexval)

    base_material_lines = []
    for hexval in color_order:
        base_material_lines.append(
            f'<base name="{hexval}" displaycolor="#{hexval}FF"/>'
        )

    materials_id = 1
    object_lines = []
    build_lines = []
    next_object_id = materials_id + 1

    for triangles, color_hex in objects:
        if not triangles:
            continue
        hexval = (color_hex or "808080").lstrip("#").upper()
        material_index = color_index[hexval]
        object_id = next_object_id
        next_object_id += 1

        vertex_lines = []
        triangle_lines = []
        v_offset = 0
        for v1, v2, v3 in triangles:
            vertex_lines.append(f'<vertex x="{v1[0]:.4f}" y="{v1[1]:.4f}" z="{v1[2]:.4f}"/>')
            vertex_lines.append(f'<vertex x="{v2[0]:.4f}" y="{v2[1]:.4f}" z="{v2[2]:.4f}"/>')
            vertex_lines.append(f'<vertex x="{v3[0]:.4f}" y="{v3[1]:.4f}" z="{v3[2]:.4f}"/>')
            triangle_lines.append(
                f'<triangle v1="{v_offset}" v2="{v_offset+1}" v3="{v_offset+2}" '
                f'pid="{materials_id}" p1="{material_index}"/>'
            )
            v_offset += 3

        object_lines.append(
            f'<object id="{object_id}" type="model" pid="{materials_id}" pindex="{material_index}">'
            f'<mesh><vertices>{"".join(vertex_lines)}</vertices>'
            f'<triangles>{"".join(triangle_lines)}</triangles></mesh></object>'
        )
        build_lines.append(f'<item objectid="{object_id}"/>')

    model_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="{materials_id}">
      {''.join(base_material_lines)}
    </basematerials>
    {''.join(object_lines)}
  </resources>
  <build>
    {''.join(build_lines)}
  </build>
</model>"""

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""

    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rel0" Target="/3D/3dmodel.model" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model_xml)


def write_plate_3mf(plate, out_path, name="plate"):
    write_3mf(plate, out_path, name=name)


def pack_plates(items, bed_x, bed_y, oversized_out=None):
    plates = []
    current = []
    x, y = BED_MARGIN, BED_MARGIN
    row_height = 0

    for triangles, color_hex in items:
        (minx, maxx), (miny, maxy), (minz, maxz) = bounding_box(triangles)
        w, d = maxx - minx, maxy - miny

        if w > bed_x - 2 * BED_MARGIN or d > bed_y - 2 * BED_MARGIN:
            if oversized_out is not None:
                oversized_out.append((w, d))
            continue

        if len(current) >= PLATE_LIMIT:
            plates.append(current)
            current = []
            x, y, row_height = BED_MARGIN, BED_MARGIN, 0

        if x + w > bed_x - BED_MARGIN:
            x = BED_MARGIN
            y += row_height + BED_MARGIN
            row_height = 0

        if y + d > bed_y - BED_MARGIN:
            plates.append(current)
            current = []
            x, y, row_height = BED_MARGIN, BED_MARGIN, 0

        dx, dy, dz = x - minx, y - miny, -minz
        placed = [
            tuple((p[0] + dx, p[1] + dy, p[2] + dz) for p in tri)
            for tri in triangles
        ]
        current.append((placed, color_hex))

        x += w + BED_MARGIN
        row_height = max(row_height, d)

    if current:
        plates.append(current)
    return plates


def convert_set(set_num, out_dir, bed_x, bed_y, progress=None, smart_rotation=False, printer_tolerance=False):
    def emit(step, done, total, detail=""):
        if progress is not None:
            progress(step, done, total, detail)
    os.makedirs(out_dir, exist_ok=True)
    if not _parts_index():
        raise ConversionError("parts library not found in ./parts")
    emit("Fetching parts list", 0, 1, set_num)
    colors = load_colors()
    emit("Loading colors", 1, 1, f"{len(colors)} colors")
    parts = get_set_parts(set_num)
    total_unique = len(parts)
    emit("Converting parts", 0, total_unique, "")

    excel_rows = []
    plate_items = []
    failed = []
    total_pieces = 0

    tolerance_mm = DEFAULT_TOLERANCE_MM if printer_tolerance else 0.0
    jobs = [(part_num, color_id, qty, colors, tolerance_mm) for (part_num, color_id), qty in parts.items()]
    results = {}
    done_count = 0
    done_lock = threading.Lock()
    workers = min(GEOMETRY_WORKERS, max(total_unique, 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(convert_one_part, job): job[0] for job in jobs}
        for future in as_completed(future_map):
            part_num, color_id, qty, outcome = future.result()
            with done_lock:
                done_count += 1
                emit("Converting parts", done_count, total_unique, part_num)
            results[(part_num, color_id)] = (qty, outcome)

    for (part_num, color_id), qty in parts.items():
        qty, outcome = results[(part_num, color_id)]
        if outcome is None:
            failed.append(part_num)
            continue
        triangles, color_info = outcome
        if smart_rotation:
            triangles = smart_orient_triangles(triangles)

        excel_rows.append({
            "part": part_num,
            "quantity": qty,
            "color_name": color_info["name"],
            "color_hex": color_info["hex"],
            "filament_suggestion": color_info["name"],
        })

        for _ in range(qty):
            plate_items.append((triangles, color_info["hex"]))
        total_pieces += qty

    if not excel_rows:
        raise ConversionError("none of this set's parts could be converted")

    emit("Converting parts", total_unique, total_unique, "done")
    emit("Packing plates", 0, 1, f"{total_pieces} pieces")
    wb = Workbook()
    ws = wb.active
    ws.title = "Parts List"
    ws.append(["Part Number", "Quantity", "Color", "Color Hex", "Suggested Filament"])
    for row in excel_rows:
        ws.append([row["part"], row["quantity"], row["color_name"], row["color_hex"], row["filament_suggestion"]])
    excel_path = os.path.join(out_dir, "parts_list.xlsx")
    wb.save(excel_path)

    oversized = []
    plates = pack_plates(plate_items, bed_x, bed_y, oversized_out=oversized)
    emit("Packing plates", 1, 1, f"{len(plates)} plates")
    plates_dir = os.path.join(out_dir, "plates")
    os.makedirs(plates_dir, exist_ok=True)
    for i, plate in enumerate(plates, start=1):
        emit("Writing plates", i - 1, len(plates), f"plate_{i}")
        plate_path = os.path.join(plates_dir, f"plate_{i}.stl")
        write_plate_stl(plate, plate_path, f"plate_{i}")
        plate_3mf_path = os.path.join(plates_dir, f"plate_{i}.3mf")
        write_plate_3mf(plate, plate_3mf_path, f"plate_{i}")
    emit("Writing plates", len(plates), max(len(plates), 1), "done")

    emit("Zipping", 0, 1, "")
    zip_path = os.path.join(out_dir, f"{set_num.replace('/', '_')}_brickstl.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(plates_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                arcname = os.path.relpath(fpath, out_dir)
                z.write(fpath, arcname)
        z.write(excel_path, "parts_list.xlsx")

    skipped = f", skipped {len(failed)}: " + ", ".join(sorted(failed)) if failed else ""
    emit("Done", 1, 1, f"{total_pieces} pieces, {len(plates)} plates" + skipped)
    return {
        "zip_path": zip_path,
        "unique_parts": len(excel_rows),
        "total_pieces": total_pieces,
        "failed": failed,
        "plate_count": len(plates),
        "oversized_count": len(oversized),
    }
