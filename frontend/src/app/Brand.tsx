// The mark is a raster export of logo.png (see frontend/public/brand/), so it is sized
// by height and left to keep its own aspect ratio rather than being boxed into a square.
export function Brand() {
  return (
    <div className="brand">
      <img className="brand-mark" src="/brand/logo.png" alt="" width={52} height={36} />
      <span className="brand-name">EasyMigrate</span>
    </div>
  )
}
