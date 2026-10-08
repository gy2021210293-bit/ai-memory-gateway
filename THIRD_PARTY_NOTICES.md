# Third-party notices

This repository contains or is based on the following third-party work. Each component remains subject to its own license notice.

## AI Memory Gateway

The gateway is based on AI Memory Gateway by 七堂伽藍_ and Midsummer. The checked-out source includes their MIT copyright and permission notice, retained in [`LICENSE`](LICENSE). This repository's initial import did not preserve the exact upstream commit, so the license at that exact source revision should be confirmed before release. A public AI Memory Gateway fork also publishes the same MIT notice; the current upstream repository identifies its current license as AGPL-3.0-only.

Upstream project: <https://github.com/garan0613/pawwake>
MIT-licensed fork carrying the same notice: <https://github.com/1205peng/ai-memory-gateway>

## Memory Constellations

The read-only constellation view adapts the canvas visualization from Memory Constellations by Clara Shafiq and Draco Malfoy. The adapted files are under `static/constellation/` and `templates/constellation.html`.

Upstream project: <https://github.com/ClaraShafiq/MemoryConstellations>
License: MIT

Copyright (c) 2026 Clara Shafiq & Draco Malfoy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Honcho

Honcho by Plastic Labs is listed as an architecture and behavior reference in the local Honcho-style adaptation guide. This repository does not intentionally include Honcho source code; no direct code reuse was identified during this review. Honcho is licensed under AGPL-3.0. If any source code or prompt text was copied or adapted from Honcho, this notice is not a substitute for resolving the applicable license obligations.

Upstream project: <https://github.com/plastic-labs/honcho>

## Drivesoid

The optional integration in `drives_integration.py` communicates with a separately deployed Drivesoid service over its HTTP API. This repository does not bundle the service or reuse its source code. The upstream currently identifies its license as CC BY-NC-SA 4.0 from version 2.1.0 onward; versions through 2.0.0 were MIT-licensed.

Upstream project: <https://github.com/A1batr055/Drivesoid>
