# Screenshot-derived brand logos

Assets: cms.png, green-foundation.png, vrutti.png, swasti.png, upfront.png.
Created from the five user-supplied screenshots using the built-in image editing tool.
These are reconstructed screenshot assets, not original vector brand masters.
Setu and Community Action Collab remain text-only until assets are supplied.

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
