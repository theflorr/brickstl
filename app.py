import os
import re
import tempfile
import threading
import traceback
import uuid

from flask import Flask, request, render_template, send_file, jsonify

import converter

app = Flask(__name__)

WORK_ROOT = os.path.join(tempfile.gettempdir(), "brickstl_web")
os.makedirs(WORK_ROOT, exist_ok=True)

PRINTERS = converter.COMMON_PRINTERS
PRINTER_LABELS = {
    "ender3": "Creality Ender 3",
    "ender3v2": "Creality Ender 3 V2",
    "ender5": "Creality Ender 5",
    "ender5plus": "Creality Ender 5 Plus",
    "creality_k1": "Creality K1",
    "prusa_mk3": "Prusa MK3",
    "prusa_mk4": "Prusa MK4",
    "bambu_a1": "Bambu Lab A1",
    "bambu_a1_mini": "Bambu Lab A1 Mini",
    "bambu_x1c": "Bambu Lab X1 Carbon",
    "bambu_p1s": "Bambu Lab P1S",
    "elegoo_neptune4": "Elegoo Neptune 4",
}
PRINTER_ORDER = [k for k in PRINTER_LABELS if k in PRINTERS]

JOBS = {}
JOBS_LOCK = threading.Lock()


def set_progress(job_id, step, done, total, detail=""):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        job["step"] = step
        job["done"] = done
        job["total"] = total
        job["detail"] = detail


def run_job(job_id, set_num, bed_x, bed_y, smart_rotation=False, printer_tolerance=False):
    try:
        job_dir = tempfile.mkdtemp(dir=WORK_ROOT)
        with JOBS_LOCK:
            JOBS[job_id]["job_dir"] = job_dir
        result = converter.convert_set(
            set_num,
            job_dir,
            bed_x,
            bed_y,
            progress=lambda s, d, t, x="": set_progress(job_id, s, d, t, x),
            smart_rotation=smart_rotation,
            printer_tolerance=printer_tolerance,
        )
        with JOBS_LOCK:
            JOBS[job_id]["state"] = "done"
            JOBS[job_id]["result"] = result
            JOBS[job_id]["step"] = "Done"
            JOBS[job_id]["done"] = 1
            JOBS[job_id]["total"] = 1
    except converter.ConversionError as e:
        with JOBS_LOCK:
            JOBS[job_id]["state"] = "error"
            JOBS[job_id]["error"] = str(e)
    except Exception:
        traceback.print_exc()
        with JOBS_LOCK:
            JOBS[job_id]["state"] = "error"
            JOBS[job_id]["error"] = "something went wrong converting that set"


def clean_set_number(raw):
    raw = raw.strip()
    if not re.match(r"^[A-Za-z0-9\-]+$", raw):
        raise converter.ConversionError("set number can only contain letters, numbers and a dash")
    return raw


@app.route("/")
def index():
    return render_template("index.html", printers=PRINTER_ORDER, labels=PRINTER_LABELS, sizes=PRINTERS)


@app.route("/convert", methods=["POST"])
def convert():
    set_num = request.form.get("set_number", "")
    printer = request.form.get("printer", "")
    custom_x = request.form.get("bed_x", "").strip()
    custom_y = request.form.get("bed_y", "").strip()
    smart_rotation = request.form.get("smart_rotation") == "1"
    printer_tolerance = request.form.get("printer_tolerance") == "1"

    try:
        set_num = clean_set_number(set_num)

        if printer == "custom":
            try:
                bed_x, bed_y = float(custom_x), float(custom_y)
                if bed_x <= 0 or bed_y <= 0:
                    raise ValueError
            except ValueError:
                raise converter.ConversionError("custom bed size must be two positive numbers")
        elif printer in PRINTERS:
            bed_x, bed_y = PRINTERS[printer]
        else:
            raise converter.ConversionError("choose a printer or enter a custom bed size")

        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {
                "state": "working",
                "step": "Starting",
                "done": 0,
                "total": 1,
                "detail": set_num,
                "set_num": set_num,
                "smart_rotation": smart_rotation,
                "printer_tolerance": printer_tolerance,
                "job_dir": None,
                "result": None,
                "error": None,
            }
        thread = threading.Thread(
            target=run_job,
            args=(job_id, set_num, bed_x, bed_y, smart_rotation, printer_tolerance),
            daemon=True,
        )
        thread.start()

        return jsonify({"job_id": job_id})

    except converter.ConversionError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "something went wrong converting that set"}), 500


@app.route("/progress/<job_id>")
def progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "unknown job"}), 404
        return jsonify({
            "state": job["state"],
            "step": job["step"],
            "done": job["done"],
            "total": job["total"],
            "detail": job["detail"],
            "error": job["error"],
        })


@app.route("/download/<job_id>")
def download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "unknown job"}), 404
        if job["state"] == "error":
            return jsonify({"error": job["error"] or "conversion failed"}), 400
        if job["state"] != "done" or not job["result"]:
            return jsonify({"error": "not ready yet"}), 409
        zip_path = job["result"]["zip_path"]
        set_num = job["set_num"]
        smart_rotation = job.get("smart_rotation", False)
        printer_tolerance = job.get("printer_tolerance", False)
        prefix = ("S" if smart_rotation else "") + ("T" if printer_tolerance else "")
    return send_file(
        zip_path,
        as_attachment=True,
        download_name=f"{prefix}{set_num}_brickstl.zip",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=25570, debug=False)
