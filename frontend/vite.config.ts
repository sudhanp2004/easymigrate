import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The wheel ships built assets from src/easymigrate/ui/static/ (spec §6/§7): `npm run
// build` must land there so `easymigrate ui` serves a working app from a plain
// `pip`/`uv` install with no node toolchain on the user's machine.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/easymigrate/ui/static',
    emptyOutDir: true,
  },
})
