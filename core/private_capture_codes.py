"""Stable, non-sensitive diagnostics for manager-owned private captures."""

PRIVATE_DESKTOP_CAPTURE_DIAGNOSTIC_CODES = frozenset({
    "capture_dpi_context_failed", "capture_window_rect_failed",
    "capture_dimensions_invalid", "capture_screen_dc_failed",
    "capture_compatible_dc_failed", "capture_bitmap_create_failed",
    "capture_bitmap_select_failed", "capture_print_window_failed",
    "capture_content_invalid", "capture_png_encode_failed",
    "capture_png_validation_failed",
})
