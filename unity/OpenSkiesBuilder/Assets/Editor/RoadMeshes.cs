// Draws an Open Skies map's roads: ribbons laid on the terrain, bridge decks with colliders, grouped per
// 5,120 m tile like the Swiss Alps map, under <root>/Roads. Called by MapBuilder after the terrain:
//     NOEarth.RoadMeshes.Build(root, buildDir, half, AssetRoot);
//
// Input, in the build folder (tools/roads.py writes the first two):
//   roads.bytes      the road network, NOROAD01/02 (NOCustomMaps RoadData, MIT): game metres, x east,
//                    y up, z north. The same file ships as the manifest's roadNetwork for the ground AI.
//   roads_draw.json  per record, in file order: width (m), lift above the ground (m), kind
//                    (0 ground, 1 tunnel, 2 bridge, 3 causeway). Optional: without it widths and lifts
//                    come from the class and kinds from the record flags.
//   heights.npy      the heights MapBuilder meshes (float32, row 0 = north edge, column 0 = west edge).
// Output:
//   Roads/road_<i>_<j>                LODGroup; LOD0 ribbons (MeshFilter + MeshRenderer, no collider:
//                                     the terrain carries the vehicles)
//     road_<i>_<j>_p<k>               further LOD0 parts (each mesh stays under 65,000 vertices)
//     road_<i>_<j>[_p<k>]_LOD1        ribbons laid on the LOD1 terrain surface (every 2nd vertex)
//     road_<i>_<j>[_p<k>]_LOD2        LOD2 surface (every 4th vertex), motorway/trunk/primary only
//   Roads/Bridges/bridge_<i>_<j>
//     deck[_p<k>]                     deck (MeshFilter, MeshRenderer, MeshCollider)
//     concrete[_p<k>]                 girder sides and underside, parapets, piers
// i counts tiles from the west, j from the south; each tile object sits at its tile's south-west corner
// (-half + i * 5120, 0, -half + j * 5120) and its vertices are local to it. Tunnels are not drawn.
// Ribbons follow the terrain mesh's own triangles (MapBuilder splits each cell from (a+1, b) to (a, b+1)),
// with a vertex wherever a road crosses a grid or diagonal line, so they neither sink nor float. The
// LODGroups switch with the terrain tiles' (same thresholds, reference point and size), so each LOD's
// ribbons lie on the surface that is drawn.
// Materials: __BORROW__Paved (NOCustomMaps swaps in the game's road surface) and __BORROW__Concrete.
// UV0: u across the road 0..1, v = metres along the road / width, continuous across record cuts.
// Data: (c) OpenStreetMap contributors, Overture Maps Foundation (ODbL).
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.Rendering;

namespace NOEarth
{
    public static class RoadMeshes
    {
        const float TileSize = 5120f;
        const int MaxVertices = 65000;
        const float SkirtDepth = 40f;                                   // MapBuilder's, for matching LOD bounds
        static readonly float[] LodHeights = { 0.45f, 0.18f, 0.05f };   // MapBuilder's terrain LODs
        static readonly int[] LodSteps = { 1, 2, 4 };
        static readonly float[] ClassWidth = { 24f, 16f, 12f, 8f };
        static readonly float[] ClassLift = { 0.48f, 0.42f, 0.36f, 0.30f };
        const float MaxRibbonStep = 25f;     // at most this far between ribbon cross-sections (x LOD step)
        const float DeckStep = 20f;          // deck cross-sections (short triangles for PhysX)
        const float GirderStep = 40f;
        const float PierSpacing = 50f;
        const float DeckOverTerrain = 0.15f; // a deck never dips below the ground plus this (bridge ends)
        const float ParapetHeight = 0.9f, ParapetThickness = 0.3f;

        [Serializable]
        class DrawData
        {
            public int version;
            public string credit;
            public int count;
            public float[] width;
            public float[] lift;
            public int[] kind;
        }

        sealed class Rec
        {
            public int Index;
            public byte Class, Flags;
            public float StartAlong, Width, Lift;
            public int Kind;
            public Vector3[] Pts;
        }

        struct Sample
        {
            public float X, Z, S;
            public Sample(float x, float z, float s) { X = x; Z = z; S = s; }
        }

        // ------------------------------------------------------------------------------------- terrain

        sealed class Ground
        {
            readonly float[,] h;
            readonly int n;
            readonly double half, pitch;

            public Ground(float[,] heights, float mapHalf)
            {
                h = heights;
                n = heights.GetLength(0);
                half = mapHalf;
                pitch = 2.0 * mapHalf / (n - 1);
            }

            public int Samples => n;
            public double Pitch => pitch;
            public float Raw(int row, int col) => h[row, col];

            // Height of the terrain mesh at LOD step 1, 2 or 4, on the same triangles MapBuilder draws.
            public float At(double x, double z, int step)
            {
                double p = pitch * step;
                int m = (n - 1) / step;
                double fc = (x + half) / p, fb = (z + half) / p;
                int c0 = Math.Max(0, Math.Min(m - 1, (int)Math.Floor(fc)));
                int b0 = Math.Max(0, Math.Min(m - 1, (int)Math.Floor(fb)));
                float fx = Mathf.Clamp01((float)(fc - c0)), fz = Mathf.Clamp01((float)(fb - b0));
                int col = c0 * step, row = (n - 1) - b0 * step;
                float h00 = h[row, col], h10 = h[row, col + step], h01 = h[row - step, col], h11 = h[row - step, col + step];
                return fx + fz <= 1f
                    ? h00 + fx * (h10 - h00) + fz * (h01 - h00)
                    : h11 + (1f - fx) * (h01 - h11) + (1f - fz) * (h10 - h11);
            }
        }

        // ------------------------------------------------------------------------------------- meshes

        // Collects strips and quads into meshes of at most MaxVertices, starting a new part when full.
        sealed class PartBuilder
        {
            readonly string baseName, suffix;
            readonly List<Vector3> v = new List<Vector3>();
            readonly List<Vector2> uv = new List<Vector2>();
            readonly List<int> t = new List<int>();
            readonly List<Mesh> done = new List<Mesh>();
            int prevStart = -1;
            Vector3[] prevV;
            Vector2[] prevUv;

            public PartBuilder(string baseName, string suffix) { this.baseName = baseName; this.suffix = suffix; }

            public int Vertices { get; private set; }

            public void EndStrip() { prevStart = -1; }

            // One cross-section of a strip; joined to the previous one by quads if the strip goes on.
            // Faces point along cross(section[j+1] - section[j], forward).
            public void Section(Vector3[] sv, Vector2[] suv)
            {
                int k = sv.Length;
                if (v.Count + k > MaxVertices)
                {
                    bool goesOn = prevStart >= 0 && prevV.Length == k;
                    var pv = prevV;
                    var puv = prevUv;
                    Flush();
                    if (goesOn)
                    {
                        prevStart = v.Count;
                        v.AddRange(pv);
                        uv.AddRange(puv);
                        prevV = pv;
                        prevUv = puv;
                    }
                }
                int start = v.Count;
                v.AddRange(sv);
                uv.AddRange(suv);
                if (prevStart >= 0 && prevV.Length == k)
                {
                    for (int j = 0; j < k - 1; j++)
                    {
                        int a = prevStart + j, b = prevStart + j + 1, c = start + j, d = start + j + 1;
                        t.Add(a); t.Add(b); t.Add(d);
                        t.Add(a); t.Add(d); t.Add(c);
                    }
                }
                prevStart = start;
                prevV = sv;
                prevUv = suv;
            }

            // A quad seen from its front as bottom-left, top-left, top-right, bottom-right.
            public void Quad(Vector3 bl, Vector3 tl, Vector3 tr, Vector3 br, float w, float h)
            {
                EndStrip();
                if (v.Count + 4 > MaxVertices) Flush();
                int s = v.Count;
                v.Add(bl); v.Add(tl); v.Add(tr); v.Add(br);
                uv.Add(new Vector2(0, 0)); uv.Add(new Vector2(0, h)); uv.Add(new Vector2(w, h)); uv.Add(new Vector2(w, 0));
                t.Add(s); t.Add(s + 1); t.Add(s + 2);
                t.Add(s); t.Add(s + 2); t.Add(s + 3);
            }

            void Flush()
            {
                prevStart = -1;
                if (t.Count == 0)
                {
                    v.Clear(); uv.Clear();
                    return;
                }
                var mesh = new Mesh
                {
                    name = baseName + (done.Count == 0 ? "" : "_p" + done.Count) + suffix,
                    indexFormat = IndexFormat.UInt16
                };
                mesh.SetVertices(v);
                mesh.SetUVs(0, uv);
                mesh.SetTriangles(t, 0, true);
                mesh.RecalculateNormals();
                mesh.RecalculateTangents();
                mesh.RecalculateBounds();
                Vertices += v.Count;
                done.Add(mesh);
                v.Clear(); uv.Clear(); t.Clear();
            }

            public List<Mesh> Finish()
            {
                Flush();
                return done;
            }
        }

        sealed class Tile
        {
            public readonly int I, J;
            public readonly Vector3 Origin;
            public readonly PartBuilder[] Lods = new PartBuilder[3];
            public PartBuilder Deck, Concrete;

            public Tile(int i, int j, float half)
            {
                I = i; J = j;
                Origin = new Vector3(-half + i * TileSize, 0f, -half + j * TileSize);
            }

            public PartBuilder Lod(int l) => Lods[l] ??= new PartBuilder($"road_{I}_{J}", l == 0 ? "" : $"_LOD{l}");
            public PartBuilder DeckParts() => Deck ??= new PartBuilder($"bridge_{I}_{J}_deck", "");
            public PartBuilder ConcreteParts() => Concrete ??= new PartBuilder($"bridge_{I}_{J}_concrete", "");
        }

        // ------------------------------------------------------------------------------------- entry

        public static void Build(GameObject root, string buildDir, float mapHalf, string assetRoot)
        {
            Build(root, buildDir, mapHalf, assetRoot, ReadNpy(Path.Combine(buildDir, "heights.npy")));
        }

        // Same, with the heights MapBuilder has already read (saves reading 100 MB twice).
        public static void Build(GameObject root, string buildDir, float mapHalf, string assetRoot, float[,] heights)
        {
            string roadsPath = Path.Combine(buildDir, "roads.bytes");
            if (!File.Exists(roadsPath))
            {
                Debug.LogWarning($"NOEARTH_ROADS no {roadsPath}: no roads drawn");
                return;
            }
            var recs = ReadRoads(roadsPath);
            ApplyDraw(recs, Path.Combine(buildDir, "roads_draw.json"));
            var ground = new Ground(heights, mapHalf);
            int tiles = Mathf.RoundToInt(2f * mapHalf / TileSize);
            var tileMap = new Dictionary<int, Tile>();
            Tile TileAt(float x, float z)
            {
                int i = Mathf.Clamp(Mathf.FloorToInt((x + mapHalf) / TileSize), 0, tiles - 1);
                int j = Mathf.Clamp(Mathf.FloorToInt((z + mapHalf) / TileSize), 0, tiles - 1);
                int key = i * 4096 + j;
                if (!tileMap.TryGetValue(key, out var tile)) tileMap[key] = tile = new Tile(i, j, mapHalf);
                return tile;
            }

            var ends = Junctions(recs);
            int ribbons = 0, bridges = 0;
            foreach (var r in recs)
            {
                if (r.Pts.Length < 2) continue;
                if (r.Kind == 0)
                {
                    float extA = Extension(r, ends, true), extB = Extension(r, ends, false);
                    // Overlapping ribbons at junctions: a few centimetres apart, so they do not fight for depth.
                    float lift = r.Lift + 0.015f * (r.Index % 3);
                    for (int l = 0; l < 3; l++)
                        if (l < 2 || r.Class < 3)
                            Ribbon(r, l, lift, extA, extB, ground, mapHalf, TileAt);
                    ribbons++;
                }
                else if (r.Kind == 2 || r.Kind == 3)
                {
                    Bridge(r, ground, TileAt);
                    bridges++;
                }
            }

            // Materials: placeholders the loader swaps for the game's own.
            var shader = Shader.Find("Unlit/Color") ?? Shader.Find("Standard");
            var paved = new Material(shader) { name = "__BORROW__Paved", color = new Color(0.32f, 0.32f, 0.33f) };
            var concrete = new Material(shader) { name = "__BORROW__Concrete", color = new Color(0.62f, 0.61f, 0.58f) };
            AssetDatabase.CreateAsset(paved, $"{assetRoot}/__BORROW__Paved.mat");
            AssetDatabase.CreateAsset(concrete, $"{assetRoot}/__BORROW__Concrete.mat");
            if (!AssetDatabase.IsValidFolder($"{assetRoot}/Meshes")) AssetDatabase.CreateFolder(assetRoot, "Meshes");

            var built = new List<(Tile tile, List<Mesh>[] lods, List<Mesh> deck, List<Mesh> conc)>();
            int vertices = 0, meshes = 0;
            foreach (var tile in tileMap.Values.OrderBy(t => t.J).ThenBy(t => t.I))
            {
                var lods = new List<Mesh>[3];
                for (int l = 0; l < 3; l++)
                {
                    lods[l] = tile.Lods[l]?.Finish() ?? new List<Mesh>();
                    vertices += tile.Lods[l]?.Vertices ?? 0;
                }
                var deck = tile.Deck?.Finish() ?? new List<Mesh>();
                var conc = tile.Concrete?.Finish() ?? new List<Mesh>();
                vertices += (tile.Deck?.Vertices ?? 0) + (tile.Concrete?.Vertices ?? 0);
                built.Add((tile, lods, deck, conc));
            }

            AssetDatabase.StartAssetEditing();
            try
            {
                foreach (var (tile, lods, deck, conc) in built)
                {
                    SaveMeshes($"{assetRoot}/Meshes/road_{tile.I}_{tile.J}.asset", lods.SelectMany(m => m));
                    SaveMeshes($"{assetRoot}/Meshes/bridge_{tile.I}_{tile.J}.asset", deck.Concat(conc));
                    meshes += lods.Sum(m => m.Count) + deck.Count + conc.Count;
                }
            }
            finally { AssetDatabase.StopAssetEditing(); }

            var roads = root.transform.Find("Roads")?.gameObject;
            if (roads == null)
            {
                roads = new GameObject("Roads") { layer = 0 };
                roads.transform.SetParent(root.transform, false);
            }
            var bridgeRoot = new GameObject("Bridges") { layer = 0 };
            bridgeRoot.transform.SetParent(roads.transform, false);
            int roadTiles = 0, bridgeTiles = 0;
            foreach (var (tile, lods, deck, conc) in built)
            {
                if (lods[0].Count > 0)
                {
                    TileObject(roads.transform, tile, lods, paved, ground, mapHalf);
                    roadTiles++;
                }
                if (deck.Count > 0 || conc.Count > 0)
                {
                    var b = new GameObject($"bridge_{tile.I}_{tile.J}") { layer = 0 };
                    b.transform.SetParent(bridgeRoot.transform, false);
                    b.transform.localPosition = tile.Origin;
                    for (int k = 0; k < deck.Count; k++)
                    {
                        var go = Child(b.transform, k == 0 ? "deck" : $"deck_p{k}", deck[k], paved, ShadowCastingMode.On);
                        go.AddComponent<MeshCollider>().sharedMesh = deck[k];
                    }
                    for (int k = 0; k < conc.Count; k++)
                        Child(b.transform, k == 0 ? "concrete" : $"concrete_p{k}", conc[k], concrete, ShadowCastingMode.On);
                    bridgeTiles++;
                }
            }
            AssetDatabase.SaveAssets();
            Debug.Log($"NOEARTH_ROADS {recs.Count} records: {ribbons} drawn on the ground, {bridges} bridges/causeways, " +
                      $"{recs.Count(r => r.Kind == 1)} tunnels not drawn; {roadTiles} road tiles, {bridgeTiles} bridge tiles, " +
                      $"{meshes} meshes, {vertices:N0} vertices");
        }

        // ------------------------------------------------------------------------------------- ribbons

        sealed class End
        {
            public int Count;
            public bool AllGround = true;
            public readonly List<Vector2> Out = new List<Vector2>();
        }

        static Dictionary<(float, float), End> Junctions(List<Rec> recs)
        {
            var ends = new Dictionary<(float, float), End>();
            foreach (var r in recs)
            {
                if (r.Pts.Length < 2) continue;
                for (int e = 0; e < 2; e++)
                {
                    var p = e == 0 ? r.Pts[0] : r.Pts[r.Pts.Length - 1];
                    var key = (p.x, p.z);
                    if (!ends.TryGetValue(key, out var end)) ends[key] = end = new End();
                    end.Count++;
                    end.AllGround &= r.Kind == 0;
                    end.Out.Add(LeavingDirection(r.Pts, e == 0));
                }
            }
            return ends;
        }

        static Vector2 LeavingDirection(Vector3[] p, bool atStart)
        {
            int n = p.Length;
            Vector3 a = atStart ? p[0] : p[n - 1];
            for (int k = 1; k < n; k++)
            {
                Vector3 b = atStart ? p[k] : p[n - 1 - k];
                var d = new Vector2(b.x - a.x, b.z - a.z);
                if (d.sqrMagnitude > 0.25f) return d.normalized;
            }
            return Vector2.zero;
        }

        // How far a ribbon reaches past its end into a junction, to cover the gaps between road ends there.
        static float Extension(Rec r, Dictionary<(float, float), End> ends, bool atStart)
        {
            var p = atStart ? r.Pts[0] : r.Pts[r.Pts.Length - 1];
            var end = ends[(p.x, p.z)];
            if (!end.AllGround || end.Count < 2) return 0f;
            float length = 0f;
            for (int k = 1; k < r.Pts.Length; k++)
                length += new Vector2(r.Pts[k].x - r.Pts[k - 1].x, r.Pts[k].z - r.Pts[k - 1].z).magnitude;
            if (end.Count == 2)
            {
                // Two roads meeting: only a bend leaves a gap.
                float straight = -Vector2.Dot(end.Out[0], end.Out[1]);
                if (straight > Mathf.Cos(25f * Mathf.Deg2Rad)) return 0f;
            }
            return Mathf.Min(0.5f * r.Width, 0.3f * length);
        }

        static void Ribbon(Rec r, int lod, float lift, float extA, float extB, Ground g, float half, Func<float, float, Tile> tileAt)
        {
            int step = LodSteps[lod];
            // Centre line with its distance along the road, extended into junctions.
            var c = new List<Vector2>(r.Pts.Length + 2);
            var s = new List<float>(r.Pts.Length + 2);
            float along = r.StartAlong;
            foreach (var p in r.Pts)
            {
                var q = new Vector2(p.x, p.z);
                if (c.Count > 0)
                {
                    float d = (q - c[c.Count - 1]).magnitude;
                    if (d < 0.01f) continue;
                    along += d;
                }
                c.Add(q);
                s.Add(along);
            }
            if (c.Count < 2) return;
            if (extA > 0f)
            {
                var d = (c[0] - c[1]).normalized;
                c.Insert(0, c[0] + d * extA);
                s.Insert(0, s[0] - extA);
            }
            if (extB > 0f)
            {
                int n = c.Count;
                var d = (c[n - 1] - c[n - 2]).normalized;
                c.Add(c[n - 1] + d * extB);
                s.Add(s[n - 1] + extB);
            }

            var samples = Densify(c, s, (float)(g.Pitch * step), half, MaxRibbonStep * step);
            int m = samples.Count;
            int across = 2 + (int)(r.Width / 12f);
            var world = new Vector3[m][];
            var uvs = new Vector2[m][];
            for (int i = 0; i < m; i++)
            {
                var (left, miter) = Normal(samples, i);
                world[i] = new Vector3[across];
                uvs[i] = new Vector2[across];
                for (int j = 0; j < across; j++)
                {
                    float f = (float)j / (across - 1);
                    float off = (f - 0.5f) * r.Width * miter;
                    float x = samples[i].X + left.x * off, z = samples[i].Z + left.y * off;
                    world[i][j] = new Vector3(x, g.At(x, z, step) + lift, z);
                    uvs[i][j] = new Vector2(f, samples[i].S / r.Width);
                }
            }
            // Each stretch between two cross-sections goes to the tile holding its middle.
            Tile current = null;
            PartBuilder pb = null;
            for (int i = 1; i < m; i++)
            {
                var tile = tileAt(0.5f * (samples[i - 1].X + samples[i].X), 0.5f * (samples[i - 1].Z + samples[i].Z));
                if (tile != current)
                {
                    pb?.EndStrip();
                    current = tile;
                    pb = tile.Lod(lod);
                    pb.EndStrip();
                    pb.Section(Local(world[i - 1], tile.Origin), uvs[i - 1]);
                }
                pb.Section(Local(world[i], tile.Origin), uvs[i]);
            }
            pb?.EndStrip();
        }

        static Vector3[] Local(Vector3[] w, Vector3 origin)
        {
            var o = new Vector3[w.Length];
            for (int j = 0; j < w.Length; j++) o[j] = w[j] - origin;
            return o;
        }

        // The lateral direction (left of travel) at a sample, and the stretch that keeps the width at a bend.
        static (Vector2 left, float miter) Normal(List<Sample> p, int i)
        {
            Vector2 prev = Vector2.zero, next = Vector2.zero;
            if (i > 0) prev = new Vector2(p[i].X - p[i - 1].X, p[i].Z - p[i - 1].Z).normalized;
            if (i < p.Count - 1) next = new Vector2(p[i + 1].X - p[i].X, p[i + 1].Z - p[i].Z).normalized;
            Vector2 dir = prev + next;
            if (dir.sqrMagnitude < 1e-6f) dir = next.sqrMagnitude > 0 ? next : prev;
            dir.Normalize();
            var reference = next.sqrMagnitude > 0 ? next : prev;
            float miter = 1f / Mathf.Max(0.5f, Vector2.Dot(dir, reference));
            return (new Vector2(-dir.y, dir.x), miter);
        }

        // Points along the centre line wherever it crosses a grid line or cell diagonal of the terrain at
        // this LOD's pitch (so the ribbon bends where the ground does), and at least every maxGap metres.
        static List<Sample> Densify(List<Vector2> c, List<float> s, float pitch, float half, float maxGap)
        {
            var o = new List<Sample> { new Sample(c[0].x, c[0].y, s[0]) };
            var ts = new List<double>();
            for (int k = 0; k + 1 < c.Count; k++)
            {
                Vector2 a = c[k], b = c[k + 1];
                double len = (b - a).magnitude;
                ts.Clear();
                double ua = (a.x + half) / pitch, ub = (b.x + half) / pitch;
                double wa = (a.y + half) / pitch, wb = (b.y + half) / pitch;
                Crossings(ua, ub, ts);
                Crossings(wa, wb, ts);
                Crossings(ua + wa, ub + wb, ts);
                ts.Add(1.0);
                ts.Sort();
                double last = 0.0, minT = 0.05 / Math.Max(len, 1e-6);
                for (int q = 0; q < ts.Count; q++)
                {
                    double t = ts[q];
                    if (t < 1.0 && (t - last < minT || 1.0 - t < minT)) continue;
                    int sub = Math.Max(1, (int)Math.Ceiling((t - last) * len / maxGap));
                    for (int u = 1; u <= sub; u++)
                    {
                        double tt = last + (t - last) * u / sub;
                        o.Add(new Sample((float)(a.x + (b.x - a.x) * tt), (float)(a.y + (b.y - a.y) * tt),
                                         (float)(s[k] + (s[k + 1] - s[k]) * tt)));
                    }
                    last = t;
                }
            }
            return o;
        }

        static void Crossings(double a, double b, List<double> ts)
        {
            if (Math.Abs(b - a) < 1e-9) return;
            double lo = Math.Min(a, b), hi = Math.Max(a, b);
            for (double k = Math.Floor(lo) + 1; k < hi; k += 1) ts.Add((k - a) / (b - a));
        }

        // ------------------------------------------------------------------------------------- bridges

        static void Bridge(Rec r, Ground g, Func<float, float, Tile> tileAt)
        {
            bool causeway = r.Kind == 3;
            float w = r.Width + (causeway ? 0f : 2f * ParapetThickness);
            float depth = causeway ? 0.8f : 1.6f;
            // Deck centre line, densified, with its height; never below the ground at the centre.
            var pts = new List<Vector3>();
            var s = new List<float>();
            float along = r.StartAlong;
            for (int k = 0; k < r.Pts.Length; k++)
            {
                var p = r.Pts[k];
                if (k > 0)
                {
                    var a = r.Pts[k - 1];
                    float d = new Vector2(p.x - a.x, p.z - a.z).magnitude;
                    if (d < 0.01f) continue;
                    int sub = Math.Max(1, Mathf.CeilToInt(d / DeckStep));
                    for (int u = 1; u <= sub; u++)
                    {
                        pts.Add(Vector3.Lerp(a, p, (float)u / sub));
                        s.Add(along + d * u / sub);
                    }
                    along += d;
                }
                else
                {
                    pts.Add(p);
                    s.Add(along);
                }
            }
            int m = pts.Count;
            if (m < 2) return;
            var samples = pts.Select((p, i) => new Sample(p.x, p.z, s[i])).ToList();
            var y = new float[m];
            for (int i = 0; i < m; i++) y[i] = Mathf.Max(pts[i].y, g.At(pts[i].x, pts[i].z, 1) + DeckOverTerrain);
            var mid = pts[m / 2];
            var tile = tileAt(mid.x, mid.z);
            var o = tile.Origin;
            var left = new Vector2[m];
            var miter = new float[m];
            for (int i = 0; i < m; i++) (left[i], miter[i]) = Normal(samples, i);
            Vector3 Edge(int i, float side, float up) =>
                new Vector3(pts[i].x + left[i].x * side * miter[i], y[i] + up, pts[i].z + left[i].y * side * miter[i]) - o;

            var deck = tile.DeckParts();
            deck.EndStrip();
            for (int i = 0; i < m; i++)
                deck.Section(new[] { Edge(i, -0.5f * w, 0f), Edge(i, 0.5f * w, 0f) },
                             new[] { new Vector2(0f, s[i] / w), new Vector2(1f, s[i] / w) });
            deck.EndStrip();

            // Girder, underside and parapets, at coarser cross-sections.
            var keep = new List<int> { 0 };
            for (int i = 1; i < m - 1; i++)
                if (s[i] - s[keep[keep.Count - 1]] >= GirderStep) keep.Add(i);
            keep.Add(m - 1);
            float hw = 0.5f * w, inner = hw - ParapetThickness;
            var faces = new List<(float side0, float up0, float side1, float up1)>
            {
                (hw, 0f, hw, -depth),                    // left outer wall (faces left)
                (-hw, -depth, -hw, 0f),                  // right outer wall (faces right)
                (hw, -depth, -hw, -depth),               // underside (faces down)
            };
            if (!causeway)
            {
                faces[0] = (hw, ParapetHeight, hw, -depth);
                faces[1] = (-hw, -depth, -hw, ParapetHeight);
                faces.Add((inner, 0f, inner, ParapetHeight));                      // left parapet, inner face
                faces.Add((inner, ParapetHeight, hw, ParapetHeight));              // left parapet top
                faces.Add((-inner, ParapetHeight, -inner, 0f));                    // right parapet, inner face
                faces.Add((-hw, ParapetHeight, -inner, ParapetHeight));            // right parapet top
            }
            var conc = tile.ConcreteParts();
            foreach (var (side0, up0, side1, up1) in faces)
            {
                conc.EndStrip();
                foreach (int i in keep)
                    conc.Section(new[] { Edge(i, side0, up0), Edge(i, side1, up1) },
                                 new[] { new Vector2(s[i] / 4f, 0f), new Vector2(s[i] / 4f, 1f) });
                conc.EndStrip();
            }

            // Piers where the deck stands clear of the ground.
            if (causeway) return;
            float total = s[m - 1] - s[0];
            for (float at = PierSpacing * 0.5f + 0.5f * (total % PierSpacing); at < total - 15f; at += PierSpacing)
            {
                if (at < 15f) continue;
                float target = s[0] + at;
                int i = 1;
                while (i < m - 1 && s[i] < target) i++;
                float f = Mathf.InverseLerp(s[i - 1], s[i], target);
                var p = Vector3.Lerp(pts[i - 1], pts[i], f);
                float top = Mathf.Lerp(y[i - 1], y[i], f) - depth;
                float bottom = g.At(p.x, p.z, 1) - 2f;
                if (top - bottom < 4f) continue;
                var fwd = new Vector3(pts[i].x - pts[i - 1].x, 0f, pts[i].z - pts[i - 1].z).normalized;
                var lat = new Vector3(-fwd.z, 0f, fwd.x);
                Box(conc, p - o, bottom, top, fwd, lat, 1.0f, Mathf.Clamp(0.3f * r.Width, 2f, 6f));
            }
        }

        // Four vertical faces of a box standing on (centre.x, centre.z) from bottom to top.
        static void Box(PartBuilder pb, Vector3 centre, float bottom, float top, Vector3 fwd, Vector3 lat, float halfAlong, float halfAcross)
        {
            var c = new Vector3(centre.x, 0f, centre.z);
            foreach (var (n, half, ext) in new[] { (fwd, halfAcross, halfAlong), (-fwd, halfAcross, halfAlong),
                                                    (lat, halfAlong, halfAcross), (-lat, halfAlong, halfAcross) })
            {
                // Seen from outside, right is cross(up, -n).
                var right = Vector3.Cross(Vector3.up, -n).normalized;
                var fc = c + n * ext;
                var bl = fc - right * half + Vector3.up * bottom;
                var br = fc + right * half + Vector3.up * bottom;
                pb.Quad(bl, bl + Vector3.up * (top - bottom), br + Vector3.up * (top - bottom), br, 2f * half / 4f, (top - bottom) / 4f);
            }
        }

        // ------------------------------------------------------------------------------------- objects

        static void TileObject(Transform parent, Tile tile, List<Mesh>[] lods, Material mat, Ground g, float half)
        {
            var go = new GameObject($"road_{tile.I}_{tile.J}") { layer = 0 };
            go.transform.SetParent(parent, false);
            go.transform.localPosition = tile.Origin;
            var levels = new LOD[3];
            for (int l = 0; l < 3; l++)
            {
                var renderers = new List<Renderer>();
                for (int k = 0; k < lods[l].Count; k++)
                {
                    string name = $"road_{tile.I}_{tile.J}" + (k == 0 ? "" : $"_p{k}") + (l == 0 ? "" : $"_LOD{l}");
                    var host = l == 0 && k == 0 ? go : null;
                    if (host == null)
                    {
                        host = new GameObject(name) { layer = 0 };
                        host.transform.SetParent(go.transform, false);
                    }
                    host.AddComponent<MeshFilter>().sharedMesh = lods[l][k];
                    var mr = host.AddComponent<MeshRenderer>();
                    mr.sharedMaterial = mat;
                    mr.shadowCastingMode = ShadowCastingMode.Off;
                    mr.receiveShadows = true;
                    renderers.Add(mr);
                }
                levels[l] = new LOD(LodHeights[l], renderers.ToArray());
            }
            var group = go.AddComponent<LODGroup>();
            group.SetLODs(levels);
            group.fadeMode = LODFadeMode.None;
            // Switch exactly when the terrain tile under it does: MapBuilder's tiles take their LODGroup
            // bounds from the LOD0 mesh, skirts included.
            int n = g.Samples;
            int per = Mathf.RoundToInt(TileSize / (float)g.Pitch);
            float lo = float.MaxValue, hi = float.MinValue;
            for (int b = 0; b <= per; b++)
            for (int a = 0; a <= per; a++)
            {
                int row = Mathf.Clamp((n - 1) - (tile.J * per + b), 0, n - 1);
                int col = Mathf.Clamp(tile.I * per + a, 0, n - 1);
                float v = g.Raw(row, col);
                lo = Mathf.Min(lo, v);
                hi = Mathf.Max(hi, v);
            }
            lo -= SkirtDepth;
            group.localReferencePoint = new Vector3(TileSize / 2f, 0.5f * (lo + hi), TileSize / 2f);
            group.size = Mathf.Max(TileSize, hi - lo);
        }

        static GameObject Child(Transform parent, string name, Mesh mesh, Material mat, ShadowCastingMode shadows)
        {
            var go = new GameObject(name) { layer = 0 };
            go.transform.SetParent(parent, false);
            go.AddComponent<MeshFilter>().sharedMesh = mesh;
            var mr = go.AddComponent<MeshRenderer>();
            mr.sharedMaterial = mat;
            mr.shadowCastingMode = shadows;
            return go;
        }

        static void SaveMeshes(string path, IEnumerable<Mesh> meshes)
        {
            bool first = true;
            foreach (var mesh in meshes)
            {
                if (first) AssetDatabase.CreateAsset(mesh, path);
                else AssetDatabase.AddObjectToAsset(mesh, path);
                first = false;
            }
        }

        // ------------------------------------------------------------------------------------- input

        static List<Rec> ReadRoads(string path)
        {
            using var f = File.OpenRead(path);
            using var r = new BinaryReader(f);
            string magic = new string(r.ReadChars(8));
            int version = magic == "NOROAD02" ? 2 : magic == "NOROAD01" ? 1 : 0;
            if (version == 0) throw new InvalidDataException($"{path} is not road data ({magic})");
            int count = r.ReadInt32();
            var recs = new List<Rec>(count);
            for (int i = 0; i < count; i++)
            {
                var rec = new Rec { Index = i, Class = r.ReadByte() };
                if (version >= 2) rec.Flags = r.ReadByte();
                r.ReadSingle();                                   // tunnelFraction: not drawn differently
                if (version >= 2) rec.StartAlong = r.ReadSingle();
                int n = r.ReadInt32();
                rec.Pts = new Vector3[n];
                for (int k = 0; k < n; k++) rec.Pts[k] = new Vector3(r.ReadSingle(), r.ReadSingle(), r.ReadSingle());
                int cls = Mathf.Clamp(rec.Class, 0, 3);
                rec.Width = ClassWidth[cls];
                rec.Lift = ClassLift[cls];
                rec.Kind = (rec.Flags & 1) != 0 ? 1 : (rec.Flags & 2) != 0 ? 2 : 0;
                recs.Add(rec);
            }
            return recs;
        }

        static void ApplyDraw(List<Rec> recs, string path)
        {
            if (!File.Exists(path))
            {
                Debug.LogWarning($"NOEARTH_ROADS no {path}: widths and lifts by class");
                return;
            }
            var d = JsonUtility.FromJson<DrawData>(File.ReadAllText(path));
            if (d == null || d.width == null || d.width.Length != recs.Count)
            {
                Debug.LogWarning($"NOEARTH_ROADS {path} does not match roads.bytes ({d?.width?.Length ?? 0} vs {recs.Count} records): widths and lifts by class");
                return;
            }
            for (int i = 0; i < recs.Count; i++)
            {
                recs[i].Width = Mathf.Max(2f, d.width[i]);
                if (d.lift != null && d.lift.Length == recs.Count) recs[i].Lift = d.lift[i];
                if (d.kind != null && d.kind.Length == recs.Count) recs[i].Kind = d.kind[i];
            }
        }

        // Minimal .npy reader: little-endian float32, C order, 2-D (as MapBuilder's).
        static float[,] ReadNpy(string path)
        {
            using var f = File.OpenRead(path);
            using var r = new BinaryReader(f);
            byte[] magic = r.ReadBytes(6);
            if (magic[0] != 0x93 || magic[1] != (byte)'N') throw new InvalidDataException("not .npy");
            byte major = r.ReadByte();
            r.ReadByte();
            int hlen = major == 1 ? r.ReadUInt16() : (int)r.ReadUInt32();
            string header = System.Text.Encoding.ASCII.GetString(r.ReadBytes(hlen));
            if (!header.Contains("'<f4'") || header.Contains("'fortran_order': True")) throw new InvalidDataException(header);
            string shape = header.Substring(header.IndexOf("'shape': (") + 10);
            shape = shape.Substring(0, shape.IndexOf(')'));
            int[] dims = shape.Split(',').Where(x => x.Trim().Length > 0).Select(x => int.Parse(x.Trim())).ToArray();
            var a = new float[dims[0], dims[1]];
            byte[] row = new byte[dims[1] * 4];
            for (int i = 0; i < dims[0]; i++)
            {
                int got = 0;
                while (got < row.Length)
                {
                    int k = f.Read(row, got, row.Length - got);
                    if (k <= 0) throw new EndOfStreamException(path);
                    got += k;
                }
                Buffer.BlockCopy(row, 0, a, i * row.Length, row.Length);
            }
            return a;
        }
    }
}
