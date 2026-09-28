# Container toolbar symbol

`container.svg`: Eigenes SSH-Updater-Container-Symbol; keine Docker-Marke.

Original vector geometry created for this project: three rectangular freight
containers, one above two others, with subtle vertical ribs. No third-party
artwork, logo, font, emoji or external resource was used. No external license
or attribution is required; this is a project asset, not Docker/Moby artwork.

Rendered at 20×20 logical pixels inside a passive 26×22 px label. The monochrome
SVG is tinted using the active toolbar palette, with high-DPI support. The
resource directory is included in the existing PyInstaller bundle. Missing
resources leave an empty slot without introducing an emoji fallback.

The toolbar paints a rounded, low-opacity palette-derived tint behind exactly
the symbol and the two existing Docker actions. It uses the toolbar's existing
theme background, including gradients. The host-selection dot, stretch space,
Stop and author area remain outside the group. No action is attached to the
symbol; no update-state or remote logic is involved.
