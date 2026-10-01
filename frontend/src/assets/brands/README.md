# Screenshot-derived brand logos

Assets: cms.png, green-foundation.png, vrutti.png, swasti.png, upfront.png,
setu.png, community-action-collab.png.
Created from user-supplied screenshots using the built-in image editing tool.
These are reconstructed screenshot assets, not original vector brand masters.
Setu and Community Action Collab use white backgrounds matching the logo frames.
Their classification keywords and filters are active; these files provide the
visual brand labels used by the filters and classification charts.

Setu and Community Action Collab extraction prompt:
"Crop out only the [logo and its original tagline] from the large central logo
shown in screenshot. Output on SOLID PURE WHITE background, not transparent.
Flat original artwork with sharp opaque solid-color lettering and strokes.
NO glow, shadows, gradients, blur, decoration or checkerboard.
Remove browser and Gmail completely. Preserve logo design and exact original
text. Tight framing with small white margins. The output is a clean faithful
logo asset for a website."
Setu text: SETU; Bridging Social Protection for All.
Community Action Collab text: COMMUNITY ACTION COLLAB; Catalysing a resilient world.

Extraction prompt used separately for each named logo:
"Extract only the [brand logo and its existing tagline] from this screenshot as
one clean tightly framed transparent PNG web asset. Remove all browser/Google
Drive UI and surrounding page, and remove any checkerboard transparency preview.
Preserve the logo's exact original lettering, colors, shapes, proportions and
existing tagline, do not redesign or invent anything. Center the full logo with
a small transparent margin. No other content."

Vrutti correction prompt:
"Correct ONLY the small bottom sentence to read exactly: 'Ensuring small producers
are 3 times more profitable.' Preserve the Vrutti logo, green color, multicolor
leaf, TM, and 'LIVELIHOOD IMPACT PARTNERS' text unchanged. Keep the number 3 orange.
No other change. Fully transparent background, no glow or texture."

Imported through BrandLabel.tsx so Vite emits the images under dist/assets and
the existing EC2 frontend deployment script copies them with the application.
