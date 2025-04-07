#!/usr/bin/env python3
"""
ZX Spectrum Image Converter – Interactive Hill Climb Only
with Pan, Brightness, Contrast, and Sharpen factors,
and background-thread neighbor computation.

Usage:
    python zx_interactive.py input.jpg output.png

Dependencies:
  - Pillow
  - numpy
  - PyQt5
"""

import sys
import os
import random
import numpy as np
from PIL import Image, ImageEnhance
from PyQt5 import QtWidgets, QtGui, QtCore

# ZX Spectrum Palette (15 colours)
ZX_PALETTE = np.array([
    [0, 0, 0],         # Black
    [0, 0, 192],       # Blue
    [192, 0, 0],       # Red
    [192, 0, 192],     # Magenta
    [0, 192, 0],       # Green
    [0, 192, 192],     # Cyan
    [192, 192, 0],     # Yellow
    [192, 192, 192],   # White
    [0, 0, 0],         # Duplicate Black (to remove)
    [0, 0, 255],       # Bright Blue
    [255, 0, 0],       # Bright Red
    [255, 0, 255],     # Bright Magenta
    [0, 255, 0],       # Bright Green
    [0, 255, 255],     # Bright Cyan
    [255, 255, 0],     # Bright Yellow
    [255, 255, 255]    # Bright White
], dtype=np.uint8)

# Remove duplicate black
unique_palette = []
for c in ZX_PALETTE:
    if not any(np.array_equal(c, u) for u in unique_palette):
        unique_palette.append(c)
ZX_PALETTE = np.array(unique_palette, dtype=np.uint8)

ZX_WIDTH = 256
ZX_HEIGHT = 192
ATTR_CELL_SIZE = 8  # 8x8 blocks

def nearest_palette_distance(block_pixels, palette):
    """Compute squared Euclidean distances between block pixels and palette colours."""
    diff = block_pixels[:, None, :] - palette[None, :, :]
    distances = np.sum(diff**2, axis=2)
    return distances

def choose_best_pair(block_pixels):
    """
    For an 8x8 block (64 pixels), choose the best ZX Spectrum colour combination.
    Returns:
      (best_colors, assignment, is_mixed)
    """
    distances = nearest_palette_distance(block_pixels, ZX_PALETTE)
    num_pixels = block_pixels.shape[0]

    best_error = None
    best_assignment = None
    best_colors = None
    is_mixed = False

    # Option 1: Single colour
    for i in range(len(ZX_PALETTE)):
        err = np.sum(distances[:, i])
        if best_error is None or err < best_error:
            best_error = err
            best_assignment = np.full(num_pixels, i)
            best_colors = (i,)
            is_mixed = False

    # Option 2: Pair of colours
    for i in range(len(ZX_PALETTE)):
        for j in range(i+1, len(ZX_PALETTE)):
            assign = np.where(distances[:, i] <= distances[:, j], i, j)
            err = np.sum(np.minimum(distances[:, i], distances[:, j]))
            if err < best_error:
                best_error = err
                best_assignment = assign
                best_colors = (i, j)
                is_mixed = True

    return best_colors, best_assignment, is_mixed

def process_block(block):
    """Process an 8x8 block; return (converted_block, is_mixed)."""
    block_pixels = block.reshape(-1, 3).astype(np.int32)
    _, assignment, is_mixed = choose_best_pair(block_pixels)
    new_block = ZX_PALETTE[assignment].reshape(ATTR_CELL_SIZE, ATTR_CELL_SIZE, 3)
    return new_block, is_mixed

def convert_canvas_to_zx(img_array):
    """
    Convert the entire 256x192 canvas.
    Returns (converted_array, mixed_count).
    """
    mixed_count = 0
    output = np.empty_like(img_array)
    for y in range(0, ZX_HEIGHT, ATTR_CELL_SIZE):
        for x in range(0, ZX_WIDTH, ATTR_CELL_SIZE):
            block = img_array[y:y+ATTR_CELL_SIZE, x:x+ATTR_CELL_SIZE, :]
            new_block, is_mixed = process_block(block)
            output[y:y+ATTR_CELL_SIZE, x:x+ATTR_CELL_SIZE, :] = new_block
            if is_mixed:
                mixed_count += 1
    return output, mixed_count

def apply_enhancements(pil_img, brightness, contrast, sharpen):
    """Apply brightness, contrast, and sharpen in sequence."""
    # Brightness
    pil_img = ImageEnhance.Brightness(pil_img).enhance(brightness)
    # Contrast
    pil_img = ImageEnhance.Contrast(pil_img).enhance(contrast)
    # Sharpen
    pil_img = ImageEnhance.Sharpness(pil_img).enhance(sharpen)
    return pil_img

def convert_with_params(original_image, x_offset, y_offset, brightness, contrast, sharpen):
    """
    Given the original full-size image, do:
      1) Convert to RGB, resize to (ZX_WIDTH+8)x(ZX_HEIGHT+8)
      2) Apply brightness, contrast, sharpen
      3) Crop [y_offset : y_offset+192, x_offset : x_offset+256]
      4) Convert to ZX palette
    Returns (PIL.Image, mixed_count).
    """
    canvas_w = ZX_WIDTH + 8
    canvas_h = ZX_HEIGHT + 8
    big_img = original_image.convert("RGB").resize((canvas_w, canvas_h), Image.LANCZOS)
    big_img = apply_enhancements(big_img, brightness, contrast, sharpen)

    arr = np.array(big_img)
    cropped = arr[y_offset : y_offset+ZX_HEIGHT, x_offset : x_offset+ZX_WIDTH, :]
    zx_arr, mixed_count = convert_canvas_to_zx(cropped)
    zx_img = Image.fromarray(zx_arr)
    return zx_img, mixed_count


# ------------------- BACKGROUND THREADS FOR NEIGHBORS -------------------

class NeighborComputeThread(QtCore.QThread):
    """
    QThread that computes the (pixmap, param_dict, mixed_count) for a proposed neighbor
    in the background, then emits a signal.
    """
    finishedSignal = QtCore.pyqtSignal(dict)  # will emit a dict of results

    def __init__(self, original_image, candidate_dict, pick_side, parent=None):
        """
        candidate_dict: The *prospective* candidate's parameters (x,y,b,c,sharpen).
        pick_side: "left" or "right" - helps the main widget know which neighbor is done.
        """
        super().__init__(parent)
        self.original_image = original_image
        self.pick_side = pick_side
        # Copy those parameters
        self.x = candidate_dict['x']
        self.y = candidate_dict['y']
        self.brightness = candidate_dict['brightness']
        self.contrast = candidate_dict['contrast']
        self.sharpen = candidate_dict['sharpen']

    def run(self):
        """
        Generate random neighbor from these parameters, compute the ZX image,
        convert to QPixmap, and emit via finishedSignal.
        """
        # We'll vary each parameter slightly in a small random range:
        # offsets in [-4..4]
        dx = random.randint(-4, 4)
        dy = random.randint(-4, 4)

        # brightness/contrast/sharpen factor in ~ [0.9..1.1]
        bright_factor = 1.0 + (random.random() - 0.5)*0.2
        cont_factor = 1.0 + (random.random() - 0.5)*0.2
        sharp_factor = 1.0 + (random.random() - 0.5)*0.2

        new_x = np.clip(self.x + dx, 0,  (ZX_WIDTH+8) - ZX_WIDTH)  # 0..8
        new_y = np.clip(self.y + dy, 0,  (ZX_HEIGHT+8) - ZX_HEIGHT) # 0..8

        new_brightness = self.brightness * bright_factor
        new_contrast   = self.contrast * cont_factor
        new_sharpen    = self.sharpen * sharp_factor

        # clamp to keep them from going out of hand
        # e.g. [0.3..3.0]
        new_brightness = float(np.clip(new_brightness, 0.3, 3.0))
        new_contrast   = float(np.clip(new_contrast, 0.3, 3.0))
        new_sharpen    = float(np.clip(new_sharpen, 0.3, 3.0))

        # Now compute
        zx_image, mixed_count = convert_with_params(
            self.original_image,
            int(new_x), int(new_y),
            new_brightness, new_contrast, new_sharpen
        )

        # Convert that PIL image to QPixmap
        data = zx_image.convert("RGB").tobytes("raw","RGB")
        qimg = QtGui.QImage(data, zx_image.width, zx_image.height, QtGui.QImage.Format_RGB888)
        pixmap = QtGui.QPixmap.fromImage(qimg)

        results = {
            'pick_side': self.pick_side,
            'params': {
                'x': int(new_x),
                'y': int(new_y),
                'brightness': new_brightness,
                'contrast': new_contrast,
                'sharpen': new_sharpen
            },
            'pixmap': pixmap,
            'mixed': mixed_count
        }
        self.finishedSignal.emit(results)


# ------------------- MAIN INTERACTIVE WIDGET -------------------

class ZXInteractive(QtWidgets.QWidget):
    """
    Shows the current "candidate" on the left and "neighbor" on the right.
    We precompute two next-neighbors (if you pick left or if you pick right).
    That way, whichever you choose, we can instantly switch to the next iteration
    with no wait.
    """

    def __init__(self, original_image, output_path):
        super().__init__()
        self.setWindowTitle("ZX Spectrum Interactive Converter (Pan/B/C/Sharpen)")

        self.original_image = original_image
        self.output_path = output_path

        self.canvas_width = ZX_WIDTH + 8
        self.canvas_height = ZX_HEIGHT + 8

        # We'll keep track of the "current" candidate vs neighbor
        # along with the param dict and an associated QPixmap + mixed count
        # But since we always start from some default candidate, let's do that:
        self.candidate = {
            'x': (self.canvas_width//2 - ZX_WIDTH//2),
            'y': (self.canvas_height//2 - ZX_HEIGHT//2),
            'brightness': 1.0,
            'contrast': 1.0,
            'sharpen': 1.0,
            'pixmap': None,
            'mixed': 0
        }
        self.neighbor = None  # will be computed synchronously for the first iteration
        self.iteration = 0
        self.zoom = True  # default to double-size

        # Precompute the candidate’s own pixmap
        base_zx, base_mixed = convert_with_params(
            self.original_image,
            self.candidate['x'],
            self.candidate['y'],
            self.candidate['brightness'],
            self.candidate['contrast'],
            self.candidate['sharpen']
        )
        self.candidate['pixmap'] = self.pil_to_qpixmap(base_zx)
        self.candidate['mixed'] = base_mixed

        # For the first iteration, we'll synchronously compute a neighbor
        self.neighbor = self.sync_generate_neighbor(self.candidate)

        # Now we also compute "leftNext" and "rightNext" in background threads
        self.leftNext = None
        self.rightNext = None
        self.spawn_threads()

        # --- UI ---
        self.leftLabel = QtWidgets.QLabel()
        self.leftLabel.setAlignment(QtCore.Qt.AlignCenter)
        self.rightLabel = QtWidgets.QLabel()
        self.rightLabel.setAlignment(QtCore.Qt.AlignCenter)

        self.chooseLeftBtn  = QtWidgets.QPushButton("Choose Left (Keep Candidate)")
        self.chooseRightBtn = QtWidgets.QPushButton("Choose Right (Adopt Neighbor)")
        self.finishBtn      = QtWidgets.QPushButton("Finish")
        self.toggleZoomBtn  = QtWidgets.QPushButton("Toggle Zoom")

        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setAlignment(QtCore.Qt.AlignCenter)

        # Layout
        topLayout = QtWidgets.QHBoxLayout()
        topLayout.addWidget(self.leftLabel)
        topLayout.addWidget(self.rightLabel)

        btnLayout = QtWidgets.QHBoxLayout()
        btnLayout.addWidget(self.chooseLeftBtn)
        btnLayout.addWidget(self.chooseRightBtn)
        btnLayout.addWidget(self.toggleZoomBtn)
        btnLayout.addWidget(self.finishBtn)

        mainLayout = QtWidgets.QVBoxLayout()
        mainLayout.addLayout(topLayout)
        mainLayout.addLayout(btnLayout)
        mainLayout.addWidget(self.statusLabel)

        self.setLayout(mainLayout)

        # Connect signals
        self.chooseLeftBtn.clicked.connect(self.choose_left)
        self.chooseRightBtn.clicked.connect(self.choose_right)
        self.finishBtn.clicked.connect(self.finish)
        self.toggleZoomBtn.clicked.connect(self.toggle_zoom)

        # Done. Show images
        self.update_images()

    def spawn_threads(self):
        """
        Spawns two background threads computing "leftNext" and "rightNext"
        from either the current candidate or the current neighbor—depending
        on the user's next pick.
        """
        # If user picks left, the "new candidate" remains self.candidate,
        # so we generate a neighbor from that.
        self.leftThread = NeighborComputeThread(
            self.original_image,
            self.candidate,
            pick_side="left"
        )
        self.leftThread.finishedSignal.connect(self.on_neighbor_ready)
        self.leftThread.start()

        # If user picks right, the "new candidate" becomes self.neighbor’s params
        self.rightThread = NeighborComputeThread(
            self.original_image,
            self.neighbor,
            pick_side="right"
        )
        self.rightThread.finishedSignal.connect(self.on_neighbor_ready)
        self.rightThread.start()

    def on_neighbor_ready(self, result_dict):
        """
        Called when a background thread finishes. We store the result in either
        self.leftNext or self.rightNext, as indicated by result_dict['pick_side'].
        """
        if result_dict['pick_side'] == "left":
            self.leftNext = result_dict
        else:
            self.rightNext = result_dict
        # We do NOT update the UI yet, because we only use these next-neighbor
        # images after the user picks.

    def sync_generate_neighbor(self, param_dict):
        """
        Synchronous version used for the very first neighbor so we can display
        something initially. We do the same random logic as the thread would.
        Returns a param dict for the neighbor.
        """
        dx = random.randint(-4, 4)
        dy = random.randint(-4, 4)
        bright_factor = 1.0 + (random.random() - 0.5)*0.2
        cont_factor   = 1.0 + (random.random() - 0.5)*0.2
        sharp_factor  = 1.0 + (random.random() - 0.5)*0.2

        new_x = np.clip(param_dict['x'] + dx, 0, self.canvas_width - ZX_WIDTH)
        new_y = np.clip(param_dict['y'] + dy, 0, self.canvas_height - ZX_HEIGHT)

        new_brightness = param_dict['brightness'] * bright_factor
        new_contrast   = param_dict['contrast']   * cont_factor
        new_sharpen    = param_dict['sharpen']    * sharp_factor

        new_brightness = float(np.clip(new_brightness, 0.3, 3.0))
        new_contrast   = float(np.clip(new_contrast, 0.3, 3.0))
        new_sharpen    = float(np.clip(new_sharpen, 0.3, 3.0))

        zx_image, mixed_count = convert_with_params(
            self.original_image,
            int(new_x), int(new_y),
            new_brightness, new_contrast, new_sharpen
        )
        # Convert to QPixmap
        data = zx_image.convert("RGB").tobytes("raw","RGB")
        qimg = QtGui.QImage(data, zx_image.width, zx_image.height, QtGui.QImage.Format_RGB888)
        pixmap = QtGui.QPixmap.fromImage(qimg)

        return {
            'x': int(new_x),
            'y': int(new_y),
            'brightness': new_brightness,
            'contrast': new_contrast,
            'sharpen': new_sharpen,
            'pixmap': pixmap,
            'mixed': mixed_count
        }

    def update_images(self):
        """Display candidate + neighbor, and update the status label."""
        # Scale them if zoom is on
        sf = 2 if self.zoom else 1
        w, h = ZX_WIDTH*sf, ZX_HEIGHT*sf

        cand_pix = self.candidate['pixmap'].scaled(w, h, QtCore.Qt.KeepAspectRatio)
        neig_pix = self.neighbor['pixmap'].scaled(w, h, QtCore.Qt.KeepAspectRatio)

        self.leftLabel.setPixmap(cand_pix)
        self.rightLabel.setPixmap(neig_pix)

        self.statusLabel.setText(
            f"Iteration {self.iteration}\n"
            f"Candidate: x={self.candidate['x']}, y={self.candidate['y']}, "
            f"b={self.candidate['brightness']:.2f}, "
            f"c={self.candidate['contrast']:.2f}, "
            f"s={self.candidate['sharpen']:.2f}, "
            f"mixed={self.candidate['mixed']}\n"
            f"Neighbor:  x={self.neighbor['x']}, y={self.neighbor['y']}, "
            f"b={self.neighbor['brightness']:.2f}, "
            f"c={self.neighbor['contrast']:.2f}, "
            f"s={self.neighbor['sharpen']:.2f}, "
            f"mixed={self.neighbor['mixed']}"
        )

    def choose_left(self):
        """
        The user chooses to keep the candidate (ignore the neighbor).
        Then we instantly switch to "leftNext" as the new neighbor,
        so the user doesn't wait.
        """
        self.iteration += 1
        # The new candidate is still self.candidate (unchanged).
        # The new neighbor is whatever "leftNext" was computing.
        if self.leftNext is not None:
            # build a param dict for neighbor from leftNext
            self.neighbor = {
                'x':         self.leftNext['params']['x'],
                'y':         self.leftNext['params']['y'],
                'brightness': self.leftNext['params']['brightness'],
                'contrast':   self.leftNext['params']['contrast'],
                'sharpen':    self.leftNext['params']['sharpen'],
                'pixmap':     self.leftNext['pixmap'],
                'mixed':      self.leftNext['mixed']
            }
        else:
            # If the thread hasn't finished (unlikely), do a quick fallback
            self.neighbor = self.sync_generate_neighbor(self.candidate)

        # Now spawn new threads for the next iteration
        self.leftNext = None
        self.rightNext = None
        self.spawn_threads()

        self.update_images()

    def choose_right(self):
        """
        The user chooses to adopt the neighbor as the new candidate.
        Then we instantly switch to "rightNext" as the next neighbor.
        """
        self.iteration += 1
        # The new candidate becomes self.neighbor
        self.candidate = self.neighbor

        # The new neighbor is whatever "rightNext" was computing
        if self.rightNext is not None:
            self.neighbor = {
                'x':         self.rightNext['params']['x'],
                'y':         self.rightNext['params']['y'],
                'brightness': self.rightNext['params']['brightness'],
                'contrast':   self.rightNext['params']['contrast'],
                'sharpen':    self.rightNext['params']['sharpen'],
                'pixmap':     self.rightNext['pixmap'],
                'mixed':      self.rightNext['mixed']
            }
        else:
            # fallback
            self.neighbor = self.sync_generate_neighbor(self.candidate)

        # Now spawn new threads for the next iteration
        self.leftNext = None
        self.rightNext = None
        self.spawn_threads()

        self.update_images()

    def toggle_zoom(self):
        self.zoom = not self.zoom
        self.update_images()

    def finish(self):
        """
        The user is done. Show a final preview, then save to disk.
        """
        # Convert the candidate to a PIL image again (we already have it in QPixmap).
        # But let's do it from scratch so there's no risk of compression artifacts.
        final_img, final_mixed = convert_with_params(
            self.original_image,
            self.candidate['x'],
            self.candidate['y'],
            self.candidate['brightness'],
            self.candidate['contrast'],
            self.candidate['sharpen']
        )

        # Save
        final_img.save(self.output_path)
        QtWidgets.QMessageBox.information(
            self,
            "Done",
            f"Saved final image with mixed_count={final_mixed} to:\n{self.output_path}"
        )
        self.close()

    def pil_to_qpixmap(self, pil_img):
        data = pil_img.convert("RGB").tobytes("raw", "RGB")
        qimg = QtGui.QImage(data, pil_img.width, pil_img.height, QtGui.QImage.Format_RGB888)
        return QtGui.QPixmap.fromImage(qimg)


# ------------------- MAIN LAUNCH -------------------

def main():
    if len(sys.argv) < 3:
        print("Usage: python zx_interactive.py input.jpg output.png")
        sys.exit(1)
    input_path = sys.argv[1]
    output_path = sys.argv[2]
    if not os.path.exists(input_path):
        print(f"Error: input file '{input_path}' not found.")
        sys.exit(1)

    # Open the image
    original_image = Image.open(input_path)

    app = QtWidgets.QApplication(sys.argv)
    window = ZXInteractive(original_image, output_path)
    window.show()
    app.exec_()

if __name__ == "__main__":
    main()
