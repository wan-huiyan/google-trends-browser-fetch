// docs/capture-gif.cjs — render docs/pipeline-pixel-animated.svg to docs/pipeline-flow.gif
//
// Reuses the puppeteer install from ~/Documents/agent-review-panel/node_modules.
// Run from that directory so node resolves puppeteer correctly:
//   cd ~/Documents/agent-review-panel && \
//     node ~/Documents/google-trends-browser-fetch/docs/capture-gif.cjs
//
// Requires: ffmpeg in PATH, an HTTP server serving the docs/ folder on port 3847.

const puppeteer = require('puppeteer');
const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const WIDTH = 920;
const HEIGHT = 420;
const FPS = 8;                          // 8fps for smooth fades
const DURATION = 11;                    // seconds — animation finishes at ~9.5s, hold ~1.5s
const TOTAL_FRAMES = FPS * DURATION;    // 88
const INTERVAL_MS = 1000 / FPS;         // 125ms

const SVG_URL = 'http://localhost:3847/pipeline-pixel-animated.svg';
const OUT_DIR = '/Users/huiyanwan/Documents/google-trends-browser-fetch/docs';
const OUTPUT_GIF = path.join(OUT_DIR, 'pipeline-flow.gif');
const FRAMES_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'pipeline-frames-'));

async function captureFrames() {
  console.log(`Launching browser, capturing ${TOTAL_FRAMES} frames at ${FPS}fps over ${DURATION}s...`);
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: WIDTH, height: HEIGHT });
  await page.goto(SVG_URL, { waitUntil: 'networkidle0' });
  await new Promise(r => setTimeout(r, 200)); // settle paint before t=0 of animation

  for (let i = 0; i < TOTAL_FRAMES; i++) {
    const framePath = path.join(FRAMES_DIR, `frame${String(i).padStart(4, '0')}.png`);
    await page.screenshot({ path: framePath });
    if (i % 10 === 0) process.stdout.write(`\r  Frame ${i + 1}/${TOTAL_FRAMES}...`);
    if (i < TOTAL_FRAMES - 1) {
      await new Promise(r => setTimeout(r, INTERVAL_MS));
    }
  }
  console.log(`\nDone capturing.`);
  await browser.close();
}

function buildGif() {
  console.log('Generating palette...');
  const paletteFile = path.join(FRAMES_DIR, 'palette.png');
  execSync(
    `ffmpeg -y -framerate ${FPS} -i "${FRAMES_DIR}/frame%04d.png" -vf "palettegen=max_colors=128:stats_mode=full" "${paletteFile}"`,
    { stdio: 'inherit' }
  );
  console.log('Encoding GIF (bayer dither)...');
  execSync(
    `ffmpeg -y -framerate ${FPS} -i "${FRAMES_DIR}/frame%04d.png" -i "${paletteFile}" -lavfi "paletteuse=dither=bayer:bayer_scale=5" "${OUTPUT_GIF}"`,
    { stdio: 'inherit' }
  );
}

function cleanup() {
  fs.rmSync(FRAMES_DIR, { recursive: true, force: true });
}

(async () => {
  try {
    await captureFrames();
    buildGif();
    cleanup();
    const size = (fs.statSync(OUTPUT_GIF).size / 1024).toFixed(0);
    console.log(`\nDone! ${OUTPUT_GIF} (${size} KB)`);
  } catch (err) {
    console.error('Error:', err.message);
    cleanup();
    process.exit(1);
  }
})();
