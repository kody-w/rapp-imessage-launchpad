import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import {spawnSync} from 'node:child_process';

const root = path.resolve('build');
const iconset = path.join(root, 'icon.iconset');
fs.mkdirSync(iconset, {recursive: true});

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function chunk(name, data) {
  const type = Buffer.from(name);
  const size = Buffer.alloc(4); size.writeUInt32BE(data.length);
  const sum = Buffer.alloc(4); sum.writeUInt32BE(crc32(Buffer.concat([type, data])));
  return Buffer.concat([size, type, data, sum]);
}
function polygon(x, y, points) {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const [a, b] = points[i], [c, d] = points[j];
    if ((b > y) !== (d > y) && x < (c - a) * (y - b) / (d - b) + a) inside = !inside;
  }
  return inside;
}
function pixel(x, y) {
  const cx = Math.max(30, Math.min(98, x)), cy = Math.max(30, Math.min(98, y));
  if ((x - cx) ** 2 + (y - cy) ** 2 > 30 ** 2) return [0, 0, 0, 0];
  const rect = (l, t, r, b) => x >= l && x <= r && y >= t && y <= b;
  const ellipse = (a, b, rx, ry) => ((x - a) / rx) ** 2 + ((y - b) / ry) ** 2 <= 1;
  const outer = rect(32, 30, 50, 100) || rect(50, 30, 72, 78) || (x >= 72 && ellipse(72, 54, 27, 24));
  const hole = rect(50, 47, 70, 62) || (x >= 70 && ellipse(70, 54.5, 12, 7.5));
  const leg = polygon(x, y, [[59, 72], [80, 68], [102, 100], [80, 100]]);
  if ((outer && !hole) || leg || ellipse(103, 27, 9, 9)) {
    const t = Math.max(0, Math.min(1, (x + y - 40) / 170));
    return [Math.round(115 + 24 * t), Math.round(239 - 75 * t), Math.round(204 + 51 * t), 255];
  }
  return [17, 27, 42, 255];
}
function png(size) {
  const raw = Buffer.alloc((size * 4 + 1) * size);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const sum = [0, 0, 0, 0];
      for (const dy of [.25, .75]) for (const dx of [.25, .75]) {
        const p = pixel((x + dx) * 128 / size, (y + dy) * 128 / size);
        p.forEach((value, index) => { sum[index] += value; });
      }
      const offset = y * (size * 4 + 1) + 1 + x * 4;
      sum.forEach((value, index) => { raw[offset + index] = Math.round(value / 4); });
    }
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0); header.writeUInt32BE(size, 4);
  header[8] = 8; header[9] = 6;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', header), chunk('IDAT', zlib.deflateSync(raw, {level: 9})), chunk('IEND', Buffer.alloc(0))]);
}
for (const size of [16, 32, 128, 256, 512]) {
  fs.writeFileSync(path.join(iconset, `icon_${size}x${size}.png`), png(size));
  fs.writeFileSync(path.join(iconset, `icon_${size}x${size}@2x.png`), png(size * 2));
}
fs.writeFileSync(path.join(root, 'icon.png'), png(1024));
if (process.platform === 'darwin') {
  const result = spawnSync('/usr/bin/iconutil', ['-c', 'icns', iconset, '-o', path.join(root, 'icon.icns')], {stdio: 'inherit'});
  if (result.status) process.exit(result.status);
}
console.log('Generated original Launchpad icon assets.');
