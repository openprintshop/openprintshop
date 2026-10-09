$fn = 120;

//(mm)
Length = 60;       // Length of the license plate //[10:230]
//(mm)
Width = 15;        // Width (height) of the license plate [1:200]
//(mm)
Thickness = 2;     // Thickness of the license plate
//(mm)
Border_height = 1; // Height of the border / text
//(mm)
Hole_size = 4;     // Hole size
Text_size = 8;     // Font size

Shape = 1;   // Plate shape: 0 = rect, 1 = rounded, 2 = pill
Ring = 1;    // Keyring hole: 0 = off, 1 = on
Border = 1;  // Contour border: 0 = off, 1 = on
Euro = 1;    // Blue euroband (left flag strip): 0 = off, 1 = on
Part = 0;    // Render part 0 all 1 body(white) 2 ink(black text+border) 3 euro(blue strip)

Text = "123 ABC";  // Text to be displayed
Text_x_coordinate = 0;  // Adjusting the x position of the text

Font = "Liberation Sans";
Font_Style = "Regular"; // [Regular, Bold, Italic, Bold Italic, Light, Medium, SemiBold, Thin]

surname_font = str(Font , ":style=", Font_Style);

// Corner radius derived from the Shape mode (rect / rounded / pill).
function corner_r() =
    Shape == 2 ? Width / 2                       // pill: fully rounded short ends (stadium)
  : Shape == 1 ? min(Width, Length) * 0.22       // rounded: soft corners
  :              1.5;                             // rect: barely-eased corners

// Euroband (blue left strip) width, derived from plate height. 0 when Euro is off.
// MUST match the frontend auto-fit math (screens_custom.js euroWidthMm).
function euro_w() = Euro ? max(10, Width * 0.5) : 0;

// Keyring hole centre on the X axis: inside the euroband when present, else near the left edge.
function hole_dx() = -Length / 2 + (Euro ? euro_w() / 2 : max(Width * 0.5, Hole_size + 2));

// Text region is to the RIGHT of the euroband; text is centred within it.
function text_left() = -Length / 2 + euro_w();
function text_cx() = Text_x_coordinate + (text_left() + Length / 2) / 2;

module key_chain() {
    r = corner_r();
    ew = euro_w();

    // BODY (white): plate MINUS the euroband footprint (so the blue sits flush), hole punched.
    if (Part == 0 || Part == 1)
    color([0.93, 0.95, 0.97])
    linear_extrude(height = Thickness)
        difference() {
            plate_shape(Length, Width, r);
            if (Euro)
                translate([-Length / 2, -Width / 2]) square([ew, Width]);
            if (Ring)
                translate([hole_dx(), 0]) circle(d = Hole_size);
        }

    // EURO (blue): left strip, clipped to the plate contour, hole punched. Flush with the body.
    if ((Part == 0 || Part == 3) && Euro)
    color([0.18, 0.43, 0.84])
    linear_extrude(height = Thickness)
        difference() {
            intersection() {
                plate_shape(Length, Width, r);
                translate([-Length / 2, -Width / 2]) square([ew, Width]);
            }
            if (Ring)
                translate([hole_dx(), 0]) circle(d = Hole_size);
        }

    // INK (black): raised contour border (decorative frame) — only when Border is on.
    if ((Part == 0 || Part == 2) && Border)
        translate([0, 0, Thickness])
        color([0.1, 0.1, 0.1])
        linear_extrude(height = Border_height)
            difference() {
                plate_shape(Length, Width, r);
                translate([0, 0, -0.01]) // avoid coplanar surfaces with the plate top
                    plate_shape(Length - 3, Width - 3, max(r - 1.5, 0.5));
            }

    // INK (black): raised text, centred in the region right of the euroband.
    if (Part == 0 || Part == 2)
    translate([text_cx(), 0, Thickness])
        color([0.1, 0.1, 0.1])
        linear_extrude(height = Border_height)
            text(Text, size = Text_size, font = surname_font, halign = "center", valign = "center");
}

// Rounded-rectangle / stadium outline. Radius is clamped so the hull stays valid for any l/w.
module plate_shape(l, w, r) {
    rr = max(0.01, min(r, w / 2 - 0.01, l / 2 - 0.01));
    hull() {
        for (i = [-1, 1], j = [-1, 1])
            translate([i * (l/2 - rr), j * (w/2 - rr)])
                circle(rr);
    }
}

// Back-compat alias for the previous module name.
module rounded_rectangle(l, w, r) { plate_shape(l, w, r); }

// Call the keychain module.
key_chain();
