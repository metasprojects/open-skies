// Builds a NOCustomMaps map bundle (.nomap) from an Open Skies build folder.
//
// Run headless:
//   Unity.exe -batchmode -nographics -quit -projectPath <this project> -executeMethod NOEarth.MapBuilder.Build
//             -mapBuildDir <build folder> -outDir <output folder> -logFile <log>
//
// The build folder holds what the Python stages write: manifest.json, heights.npy (float32,
// row 0 = north edge, column 0 = west edge, one sample per vertex at vertexPitch metres),
// airbases.json + airbases.bytes, and the PNG textures. The bundle mirrors the layout
// NOCustomMaps reads (see its MapManifest, MaterialMarkers and BundleLoader, MIT):
//   <RootPrefab>
//     Terrain/tile_<i>_<j>       (layer 6, MeshFilter + MeshRenderer with an empty slot, MeshCollider, LODGroup)
//       tile_<i>_<j>_LOD1, _LOD2
//     Airfields/<airbase>/runway (material __BORROW__Runway)
// plus the textures and TextAssets the manifest names, and the manifest itself as TextAsset "map".
// No game types go into the bundle.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.Rendering;

namespace NOEarth
{
    public static class MapBuilder
    {
        const string AssetRoot = "Assets/Map";
        const float TileSize = 5120f;
        const int TerrainLayer = 6;
        const float SkirtDepth = 40f;
        static readonly float[] LodHeights = { 0.45f, 0.18f, 0.05f };
        static readonly int[] LodSteps = { 1, 2, 4 };
        const float SeaTileDepth = 12f;   // a tile whose highest sample is this far under water is open sea
        // Open-sea tiles: one mesh at 256 m spacing (21 x 21 vertices). PhysX refuses to cook a collider with any
        // triangle edge over 500 m (tolerance length 1), so 512 m spacing (724 m diagonals) left 1,625 sea tiles
        // with no collider in the v0.2 build. 256 m gives 362 m diagonals. Must divide perTile (100).
        const int SeaStep = 5;

        [Serializable] class Assets { public string rootPrefab, terrainRoot, oceanBasecolor, oceanDepthmap, terrainColorMap,
            terrainMacroColor, splatGrass, splatRock, splatLush, splatFields, airbases, mapImage, seaLanes, roadNetwork,
            cityPlacements, treePositions; }
        [Serializable] class Manifest { public string mapId; public float mapSizeX; public Assets assets; }
        [Serializable] class Runway { public float startX, startZ, endX, endZ, width; public string leIdent, heIdent; }
        [Serializable] class Airbase { public string uniqueName; public float x, y, z, heading, runwayLength, runwayWidth,
            flatHalfAlong, flatHalfAcross; public int apronSide; public string leIdent, heIdent; public Runway[] extraRunways; }
        [Serializable] class AirbaseList { public Airbase[] items; }

        public static void Build()
        {
            string buildDir = Arg("-mapBuildDir"), outDir = Arg("-outDir");
            if (buildDir == null || outDir == null) throw new ArgumentException("need -mapBuildDir and -outDir");
            BuildMap(buildDir, outDir);
        }

        static string Arg(string name)
        {
            string[] a = Environment.GetCommandLineArgs();
            int i = Array.IndexOf(a, name);
            return i >= 0 && i + 1 < a.Length ? a[i + 1] : null;
        }

        public static void BuildMap(string buildDir, string outDir)
        {
            string manifestJson = File.ReadAllText(Path.Combine(buildDir, "manifest.json"));
            var m = JsonUtility.FromJson<Manifest>(manifestJson);
            string id = m.mapId;
            float size = m.mapSizeX;
            float half = size / 2f;
            int tiles = Mathf.RoundToInt(size / TileSize);

            if (AssetDatabase.IsValidFolder(AssetRoot)) AssetDatabase.DeleteAsset(AssetRoot);
            AssetDatabase.CreateFolder("Assets", "Map");
            AssetDatabase.CreateFolder(AssetRoot, "Meshes");

            // TextAssets and textures first, so their importers run in one pass.
            File.WriteAllText(Path.Combine(AssetRoot, "map.json"), manifestJson);
            File.Copy(Path.Combine(buildDir, "airbases.bytes"), Path.Combine(AssetRoot, m.assets.airbases + ".bytes"), true);
            // Optional polyline layers, read by the loader as TextAssets named in the manifest.
            if (!string.IsNullOrEmpty(m.assets.seaLanes) && File.Exists(Path.Combine(buildDir, "sealanes.bytes")))
                File.Copy(Path.Combine(buildDir, "sealanes.bytes"), Path.Combine(AssetRoot, m.assets.seaLanes + ".bytes"), true);
            if (!string.IsNullOrEmpty(m.assets.roadNetwork) && File.Exists(Path.Combine(buildDir, "roads.bytes")))
                File.Copy(Path.Combine(buildDir, "roads.bytes"), Path.Combine(AssetRoot, m.assets.roadNetwork + ".bytes"), true);
            // Towns (NOCITY01 placements of the game's own city blocks) and trees, when their stages have run.
            if (!string.IsNullOrEmpty(m.assets.cityPlacements) && File.Exists(Path.Combine(buildDir, "cities.bytes")))
                File.Copy(Path.Combine(buildDir, "cities.bytes"), Path.Combine(AssetRoot, m.assets.cityPlacements + ".bytes"), true);
            if (!string.IsNullOrEmpty(m.assets.treePositions) && File.Exists(Path.Combine(buildDir, "trees.bytes")))
                File.Copy(Path.Combine(buildDir, "trees.bytes"), Path.Combine(AssetRoot, m.assets.treePositions + ".bytes"), true);
            var textures = new (string file, string name, bool srgb, bool mips, bool readable, TextureImporterFormat fmt, int max)[]
            {
                ("macro.png", m.assets.terrainMacroColor, true, true, false, TextureImporterFormat.DXT1, 8192),
                ("map.png", m.assets.mapImage, true, true, false, TextureImporterFormat.DXT1, 8192),
                ("splat_grass.png", m.assets.splatGrass, true, true, false, TextureImporterFormat.DXT1, 4096),
                ("splat_rock.png", m.assets.splatRock, true, true, false, TextureImporterFormat.DXT1, 4096),
                ("splat_lush.png", m.assets.splatLush, true, true, false, TextureImporterFormat.DXT1, 4096),
                ("splat_fields.png", m.assets.splatFields, true, true, false, TextureImporterFormat.DXT1, 4096),
                ("ocean_depth.png", m.assets.oceanDepthmap, true, true, false, TextureImporterFormat.DXT1, 2048),
                ("ocean_basecolor.png", m.assets.oceanBasecolor, true, true, false, TextureImporterFormat.DXT1, 2048),
                ("color.png", m.assets.terrainColorMap, true, true, true, TextureImporterFormat.RGB24, 1024),
                ("grass_mask.png", id + "_grass_mask", false, false, false, TextureImporterFormat.R8, 4096),
            };
            foreach (var t in textures)
                File.Copy(Path.Combine(buildDir, t.file), Path.Combine(AssetRoot, t.name + ".png"), true);
            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
            foreach (var t in textures)
            {
                var imp = (TextureImporter)AssetImporter.GetAtPath(Path.Combine(AssetRoot, t.name + ".png"));
                // The tactical map (M key and minimap) is read as a Sprite named like the manifest's mapImage;
                // without one the loader finds nothing and the game draws the map plain green.
                bool chart = t.name == m.assets.mapImage;
                imp.textureType = chart ? TextureImporterType.Sprite : TextureImporterType.Default;
                if (chart)
                {
                    imp.spriteImportMode = SpriteImportMode.Single;
                    imp.spritePixelsPerUnit = 100f;
                }
                imp.sRGBTexture = t.srgb;
                imp.mipmapEnabled = t.mips;
                imp.isReadable = t.readable;
                imp.wrapMode = TextureWrapMode.Clamp;
                imp.filterMode = FilterMode.Bilinear;
                imp.maxTextureSize = t.max;
                imp.npotScale = TextureImporterNPOTScale.None;
                imp.alphaSource = TextureImporterAlphaSource.None;
                var def = imp.GetDefaultPlatformTextureSettings();
                def.maxTextureSize = t.max;
                def.textureCompression = TextureImporterCompression.Compressed;
                imp.SetPlatformTextureSettings(def);
                // An explicit format is only honoured as a platform override: the game is Windows x64.
                var ps = imp.GetPlatformTextureSettings("Standalone");
                ps.overridden = true;
                ps.format = t.fmt;
                ps.maxTextureSize = t.max;
                imp.SetPlatformTextureSettings(ps);
                imp.SaveAndReimport();
            }

            float[,] h = ReadNpy(Path.Combine(buildDir, "heights.npy"));
            int n = h.GetLength(0);
            float pitch = size / (n - 1);
            int perTile = Mathf.RoundToInt(TileSize / pitch);

            var root = new GameObject(m.assets.rootPrefab);
            var terrain = new GameObject(string.IsNullOrEmpty(m.assets.terrainRoot) ? "Terrain" : m.assets.terrainRoot);
            terrain.transform.SetParent(root.transform, false);

            // Open-sea tiles (every sample at least SeaTileDepth under water) are seen only through the water and
            // touched by nothing: one coarse mesh, no extra LODs. Most of a coastal map is sea, so this is most of
            // the bundle's meshes and cooked colliders.
            var seaTile = new bool[tiles, tiles];
            int seaTiles = 0;
            for (int j = 0; j < tiles; j++)
            for (int i = 0; i < tiles; i++)
            {
                float top = float.MinValue;
                for (int b = 0; b <= perTile; b++)
                for (int a = 0; a <= perTile; a++)
                    top = Math.Max(top, h[(n - 1) - (j * perTile + b), i * perTile + a]);
                seaTile[i, j] = top < -SeaTileDepth;
                if (seaTile[i, j]) seaTiles++;
            }

            AssetDatabase.StartAssetEditing();
            var meshes = new Mesh[tiles, tiles, 3];
            try
            {
                for (int j = 0; j < tiles; j++)
                for (int i = 0; i < tiles; i++)
                {
                    string path = $"{AssetRoot}/Meshes/tile_{i}_{j}.asset";
                    if (seaTile[i, j])
                    {
                        meshes[i, j, 0] = TileMesh(h, i, j, perTile, pitch, half, size, SeaStep, $"tile_{i}_{j}");
                        AssetDatabase.CreateAsset(meshes[i, j, 0], path);
                        continue;
                    }
                    for (int l = 0; l < 3; l++)
                        meshes[i, j, l] = TileMesh(h, i, j, perTile, pitch, half, size, LodSteps[l], $"tile_{i}_{j}" + (l == 0 ? "" : $"_LOD{l}"));
                    AssetDatabase.CreateAsset(meshes[i, j, 0], path);
                    AssetDatabase.AddObjectToAsset(meshes[i, j, 1], path);
                    AssetDatabase.AddObjectToAsset(meshes[i, j, 2], path);
                }
            }
            finally { AssetDatabase.StopAssetEditing(); }
            AssetDatabase.SaveAssets();
            Debug.Log($"NOEARTH tiles: {tiles * tiles - seaTiles} land/coast, {seaTiles} open sea (coarse)");

            for (int j = 0; j < tiles; j++)
            for (int i = 0; i < tiles; i++)
            {
                var tile = new GameObject($"tile_{i}_{j}") { layer = TerrainLayer };
                tile.transform.SetParent(terrain.transform, false);
                tile.transform.localPosition = new Vector3(-half + i * TileSize, 0f, -half + j * TileSize);
                var r0 = AddRenderer(tile, meshes[i, j, 0]);
                tile.AddComponent<MeshCollider>().sharedMesh = meshes[i, j, 0];
                LOD[] lods;
                if (seaTile[i, j])
                {
                    lods = new[] { new LOD(LodHeights[2], new Renderer[] { r0 }) };
                }
                else
                {
                    lods = new LOD[3];
                    lods[0] = new LOD(LodHeights[0], new Renderer[] { r0 });
                    for (int l = 1; l < 3; l++)
                    {
                        var child = new GameObject($"tile_{i}_{j}_LOD{l}") { layer = TerrainLayer };
                        child.transform.SetParent(tile.transform, false);
                        lods[l] = new LOD(LodHeights[l], new Renderer[] { AddRenderer(child, meshes[i, j, l]) });
                    }
                }
                var group = tile.AddComponent<LODGroup>();
                group.SetLODs(lods);
                group.fadeMode = LODFadeMode.None;
                group.RecalculateBounds();
            }

            BuildAirfields(root, buildDir);
            if (File.Exists(Path.Combine(buildDir, "roads.bytes")))
                RoadMeshes.Build(root, buildDir, half, AssetRoot, h);

            string prefabPath = $"{AssetRoot}/{m.assets.rootPrefab}.prefab";
            PrefabUtility.SaveAsPrefabAsset(root, prefabPath);
            UnityEngine.Object.DestroyImmediate(root);
            AssetDatabase.SaveAssets();

            PlayerSettings.bakeCollisionMeshes = true;
            Directory.CreateDirectory(outDir);
            var names = AssetDatabase.FindAssets("", new[] { AssetRoot })
                .Select(AssetDatabase.GUIDToAssetPath).Where(p => !AssetDatabase.IsValidFolder(p)).Distinct().ToArray();
            var builds = new[] { new AssetBundleBuild { assetBundleName = id + ".nomap", assetNames = names } };
            var manifest = BuildPipeline.BuildAssetBundles(outDir, builds,
                BuildAssetBundleOptions.ChunkBasedCompression | BuildAssetBundleOptions.StrictMode, BuildTarget.StandaloneWindows64);
            if (manifest == null) throw new Exception("BuildAssetBundles failed");
            Debug.Log($"NOEARTH_BUILD_OK {Path.Combine(outDir, id + ".nomap")} tiles={tiles}x{tiles} assets={names.Length}");

            // The same build uncompressed, for the release .nomapz (NOCustomMaps 1.4+): the packer cuts it into
            // 1 MiB Brotli chunks and stamps it with the SHA-256 of the .nomap above, so both files register as
            // the same cm.<mapId>.<hash8> and missions made against either work with both.
            string rawDir = Path.Combine(outDir, "raw");
            Directory.CreateDirectory(rawDir);
            var raw = BuildPipeline.BuildAssetBundles(rawDir, builds,
                BuildAssetBundleOptions.UncompressedAssetBundle | BuildAssetBundleOptions.StrictMode, BuildTarget.StandaloneWindows64);
            if (raw == null) throw new Exception("BuildAssetBundles (uncompressed) failed");
            Debug.Log($"NOEARTH_RAW_OK {Path.Combine(rawDir, id + ".nomap")}");
        }

        static MeshRenderer AddRenderer(GameObject go, Mesh mesh)
        {
            go.AddComponent<MeshFilter>().sharedMesh = mesh;
            var r = go.AddComponent<MeshRenderer>();
            r.sharedMaterials = new Material[1];   // empty slot: NOCustomMaps gives it the game's terrain material
            r.shadowCastingMode = ShadowCastingMode.On;
            return r;
        }

        // One tile at one level of detail: a regular grid in tile-local metres, normals from the
        // whole-map heights (so seams match), UV0 = position across the whole map, plus a skirt
        // hanging SkirtDepth below every edge to hide cracks between neighbours at different LODs.
        static Mesh TileMesh(float[,] h, int ti, int tj, int perTile, float pitch, float half, float size, int step, string name)
        {
            int n = h.GetLength(0);
            int q = perTile / step + 1;
            var verts = new List<Vector3>(q * q + 4 * q);
            var norms = new List<Vector3>(verts.Capacity);
            var uvs = new List<Vector2>(verts.Capacity);
            var tris = new List<int>((q - 1) * (q - 1) * 6 + 4 * (q - 1) * 6);

            for (int b = 0; b < q; b++)
            for (int a = 0; a < q; a++)
            {
                int col = ti * perTile + a * step;
                int row = (n - 1) - (tj * perTile + b * step);
                float y = h[row, col];
                float lx = a * step * pitch, lz = b * step * pitch;
                verts.Add(new Vector3(lx, y, lz));
                float dhdx = (h[row, Math.Min(col + 1, n - 1)] - h[row, Math.Max(col - 1, 0)]) / (2 * pitch);
                float dhdz = (h[Math.Max(row - 1, 0), col] - h[Math.Min(row + 1, n - 1), col]) / (2 * pitch);
                norms.Add(new Vector3(-dhdx, 1f, -dhdz).normalized);
                uvs.Add(new Vector2((-half + ti * TileSize + lx + half) / size, (-half + tj * TileSize + lz + half) / size));
            }
            for (int b = 0; b < q - 1; b++)
            for (int a = 0; a < q - 1; a++)
            {
                int v = b * q + a;
                tris.Add(v); tris.Add(v + q); tris.Add(v + 1);
                tris.Add(v + 1); tris.Add(v + q); tris.Add(v + q + 1);
            }

            void Skirt(Func<int, int> edge, bool flip)
            {
                int start = verts.Count;
                for (int k = 0; k < q; k++)
                {
                    int e = edge(k);
                    verts.Add(verts[e] - new Vector3(0, SkirtDepth, 0));
                    norms.Add(norms[e]);
                    uvs.Add(uvs[e]);
                }
                for (int k = 0; k < q - 1; k++)
                {
                    int a0 = edge(k), a1 = edge(k + 1), s0 = start + k, s1 = start + k + 1;
                    if (flip) { tris.Add(a0); tris.Add(a1); tris.Add(s0); tris.Add(a1); tris.Add(s1); tris.Add(s0); }
                    else { tris.Add(a0); tris.Add(s0); tris.Add(a1); tris.Add(a1); tris.Add(s0); tris.Add(s1); }
                }
            }
            Skirt(k => k, false);                    // south edge
            Skirt(k => (q - 1) * q + k, true);       // north edge
            Skirt(k => k * q, true);                 // west edge
            Skirt(k => k * q + (q - 1), false);      // east edge

            var mesh = new Mesh { name = name, indexFormat = verts.Count > 65535 ? IndexFormat.UInt32 : IndexFormat.UInt16 };
            mesh.SetVertices(verts);
            mesh.SetNormals(norms);
            mesh.SetUVs(0, uvs);
            mesh.SetTriangles(tris, 0, true);
            mesh.RecalculateTangents();
            mesh.RecalculateBounds();
            return mesh;
        }

        // Airfields, laid out the way NOCustomMaps reads them (its MapFixups/AirfieldLayout and the Swiss Alps bundle):
        //   Airfields/<base>/runway, runway_<k>   __BORROW__Runway, 7 cm over the levelled ground, UVs for the game's
        //                                          runway1 texture (about 55 m across, 442 m along per repeat)
        //   Airfields/<base>/tarmac               __BORROW__Tarmac, 6 cm up: the parallel taxiway, three links to the
        //                                          runway and the apron the mission's hangars stand on (asphalt UVs,
        //                                          22.3 by 177 m), on the side airbases.json names (apronSide)
        //   Airfields/<base>/<base>_floor         invisible MeshCollider 1 cm over the levelled ground
        //   AirfieldTireMarks/<base>              __TIREMARKS__Runway at each touchdown zone, shipped switched off
        //   AirfieldMarkings/<base>               __MARKINGS__Runway: runway numbers, threshold, touchdown and
        //                                          aiming-point bars, centre and edge lines, yellow taxi lines; 9 cm up,
        //                                          UVs into the game's runway_markings atlas, white vertex colours,
        //                                          shipped switched off (the loader swaps the material and shows it)
        // Taxiway and apron offsets follow the loader's AirfieldLayout: taxiway centre at runway half-width + 125 m,
        // 30 m wide; apron from there out to 120 m inside the levelled rectangle.
        static Material Placeholder(string name)
        {
            var shader = Shader.Find("Unlit/Color") ?? Shader.Find("Standard") ?? Shader.Find("Sprites/Default");
            var mat = new Material(shader) { name = name };
            AssetDatabase.CreateAsset(mat, $"{AssetRoot}/{name}.mat");
            return mat;
        }

        struct Rect2
        {
            public float a0, a1, c0, c1;
            public Rect2(float a0, float a1, float c0, float c1) { this.a0 = a0; this.a1 = a1; this.c0 = c0; this.c1 = c1; }
        }

        static void BuildAirfields(GameObject root, string buildDir)
        {
            string path = Path.Combine(buildDir, "airbases.json");
            if (!File.Exists(path)) return;
            var list = JsonUtility.FromJson<AirbaseList>("{\"items\":" + File.ReadAllText(path) + "}");
            var runwayMat = Placeholder("__BORROW__Runway");
            var tarmacMat = Placeholder("__BORROW__Tarmac");
            var tireMat = Placeholder("__TIREMARKS__Runway");
            var fields = new GameObject("Airfields");
            fields.transform.SetParent(root.transform, false);
            var tires = new GameObject("AirfieldTireMarks");
            tires.transform.SetParent(root.transform, false);
            var marksMat = Placeholder("__MARKINGS__Runway");
            var marks = new GameObject("AirfieldMarkings");
            marks.transform.SetParent(root.transform, false);
            int paved = 0, painted = 0;
            Func<float, float, Vector2> runwayUv = (al, ac) => new Vector2(0.5f + ac / 55f, al / 442f);
            foreach (var ab in list.items)
            {
                var g = new GameObject(ab.uniqueName);
                g.transform.SetParent(fields.transform, false);
                float hr = ab.heading * Mathf.Deg2Rad;
                var dir = new Vector2(Mathf.Sin(hr), Mathf.Cos(hr));
                float sideSign = ab.apronSide < 0 ? -1f : 1f;
                var across = new Vector2(dir.y, -dir.x) * sideSign;     // towards the apron
                var c = new Vector2(ab.x, ab.z);
                float L = ab.runwayLength, W = ab.runwayWidth;

                AddSurface(g, "runway", c, dir, across, new[] { new Rect2(-L / 2, L / 2, -W / 2, W / 2) }, ab.y + 0.07f,
                           runwayMat, runwayUv, 25f, true, true);
                int k = 1;
                foreach (var r in ab.extraRunways ?? Array.Empty<Runway>())
                {
                    var a = new Vector2(r.startX, r.startZ);
                    var b = new Vector2(r.endX, r.endZ);
                    var d2 = (b - a).normalized;
                    float hl = (b - a).magnitude / 2;
                    AddSurface(g, $"runway_{k++}", (a + b) / 2f, d2, new Vector2(d2.y, -d2.x),
                               new[] { new Rect2(-hl, hl, -r.width / 2, r.width / 2) }, ab.y + 0.07f, runwayMat, runwayUv,
                               25f, true, true);
                }

                // Taxiway, three links to the runway, apron.
                float twIn = W / 2 + 110f, twOut = W / 2 + 140f;
                float apronOut = Mathf.Min(twOut + 240f, ab.flatHalfAcross - 120f);
                float apronAlong = Mathf.Min(0.55f * ab.flatHalfAlong, 0.15f * ab.flatHalfAlong + 420f);
                float tEnd = L / 2 - 60f;
                var rects = new List<Rect2> { new Rect2(-tEnd, tEnd, twIn, twOut) };
                foreach (float at in new[] { -tEnd, 0f, tEnd })
                    rects.Add(new Rect2(at - 15f, at + 15f, W / 2 - 1f, twIn));
                if (apronOut - twOut >= 120f)
                    rects.Add(new Rect2(-apronAlong, apronAlong, twOut, apronOut));
                AddSurface(g, "tarmac", c, dir, across, rects.ToArray(), ab.y + 0.06f, tarmacMat,
                           (al, ac) => new Vector2(ac / 22.3f, al / 177f), 25f, true, true);

                // Floor: collider only, over the whole levelled rectangle.
                if (ab.flatHalfAlong > 0 && ab.flatHalfAcross > 0)
                    AddSurface(g, ab.uniqueName + "_floor", c, dir, across,
                               new[] { new Rect2(-ab.flatHalfAlong, ab.flatHalfAlong, -ab.flatHalfAcross, ab.flatHalfAcross) },
                               ab.y + 0.01f, null, null, 100f, false, true);

                // Tire marks at both touchdown zones, 150-750 m in, half the runway's width.
                var tm = AddSurface(tires, ab.uniqueName, c, dir, across,
                                    new[] { new Rect2(L / 2 - 750f, L / 2 - 150f, -W / 4, W / 4),
                                            new Rect2(-L / 2 + 150f, -L / 2 + 750f, -W / 4, W / 4) },
                                    ab.y + 0.075f, tireMat,
                                    (al, ac) => new Vector2(ac / (W / 2) + 0.5f, Mathf.Repeat(Mathf.Abs(al), 600f) / 600f),
                                    50f, true, false);
                if (tm != null) tm.GetComponent<MeshRenderer>().enabled = false;      // the loader switches them on

                // Paint: every runway's markings and the taxiway's yellow lines, one mesh per airfield.
                var paint = new Paint();
                PaintRunway(paint, c - dir * (L / 2), c + dir * (L / 2), W, ab.leIdent, ab.heIdent);
                foreach (var r in ab.extraRunways ?? Array.Empty<Runway>())
                    PaintRunway(paint, new Vector2(r.startX, r.startZ), new Vector2(r.endX, r.endZ), r.width, r.leIdent, r.heIdent);
                float twMid = (twIn + twOut) / 2;
                paint.Line(c + across * twMid - dir * tEnd, c + across * twMid + dir * tEnd, 0.5f, AtlasYellow);
                foreach (float at in new[] { -tEnd, 0f, tEnd })
                    paint.Line(c + dir * at + across * (W / 2 + 3f), c + dir * at + across * twMid, 0.5f, AtlasYellow);
                var pm = paint.Build(marks, ab.uniqueName, ab.y + 0.09f, marksMat);
                if (pm != null) { pm.GetComponent<MeshRenderer>().enabled = false; painted++; }  // the loader switches it on
                paved++;
            }
            Debug.Log($"NOEARTH airfields: {paved} drawn (runway, taxiway, apron, floor, tire marks), {painted} painted");
        }

        // Runway paint, mapped onto the game's runway_markings atlas (runway_markings_b, 2048 px square, shader
        // vertexColorAlphaClip: texture times vertex colour, alpha-clipped). Atlas columns, in Unity UVs (v up):
        //   digits 0-9 in five rows down the left fifth; a plain bar (threshold and touchdown bars) at u .347-.432;
        //   a wide block (aiming points) at u .451-.647; a full-height white line at u .660-.687 and a yellow one
        //   at u .702-.729, both tiling along v. Measured from the game's own texture (2026-11-07).
        static readonly int[] DigitLeftPx = { 250, 41, 176, 37, 243, 38, 250, 31, 230, 37 };
        static readonly int[] DigitRightPx = { 431, 138, 350, 213, 423, 212, 426, 197, 406, 212 };
        static readonly int[] DigitRowTopPx = { 1721, 46, 46, 465, 465, 883, 883, 1302, 1302, 1721 };   // rows 277 px tall
        const float DigitFullPx = 181f;
        static readonly Vector2 AtlasBar = new Vector2(0.350f, 0.429f);
        static readonly Vector2 AtlasBlock = new Vector2(0.454f, 0.644f);
        static readonly Vector2 AtlasWhite = new Vector2(0.662f, 0.684f);
        static readonly Vector2 AtlasYellow = new Vector2(0.704f, 0.727f);

        // Quads on the ground in world x/z, gathered into one mesh with UVs and white vertex colours.
        class Paint
        {
            readonly List<Vector3> v = new List<Vector3>();
            readonly List<Vector2> uv = new List<Vector2>();
            readonly List<Color32> col = new List<Color32>();
            readonly List<int> tri = new List<int>();

            // A rectangle from origin o: len along unit f, wid along g = right of f. u runs u0..u1 across (along g),
            // v runs v0..v1 along f. Split along f every 50 m.
            public void Quad(Vector2 o, Vector2 f, float len, float wid, float u0, float u1, float v0, float v1)
            {
                var g = new Vector2(f.y, -f.x);
                int n = Mathf.Max(1, Mathf.CeilToInt(len / 50f));
                int b = v.Count;
                for (int i = 0; i <= n; i++)
                {
                    float k = (float)i / n;
                    Vector2 p = o + f * (len * k), q = p + g * wid;
                    float vv = Mathf.Lerp(v0, v1, k);
                    v.Add(new Vector3(p.x, 0f, p.y)); uv.Add(new Vector2(u0, vv));
                    v.Add(new Vector3(q.x, 0f, q.y)); uv.Add(new Vector2(u1, vv));
                    col.Add(new Color32(255, 255, 255, 255)); col.Add(new Color32(255, 255, 255, 255));
                }
                // g is right of f, so (p_i, p_i+1, q_i) is clockwise seen from above: the face points up.
                for (int i = 0; i < n; i++)
                {
                    int a = b + 2 * i;
                    tri.Add(a); tri.Add(a + 2); tri.Add(a + 1);
                    tri.Add(a + 1); tri.Add(a + 2); tri.Add(a + 3);
                }
            }

            // A line of width w centred on a..b, from one of the tiling atlas columns (about 40 m per repeat).
            public void Line(Vector2 a, Vector2 b, float w, Vector2 column)
            {
                float len = (b - a).magnitude;
                if (len < 1f) return;
                var f = (b - a) / len;
                var g = new Vector2(f.y, -f.x);
                Quad(a - g * (w / 2), f, len, w, column.x, column.y, 0f, len / 40f);
            }

            public GameObject Build(GameObject parent, string name, float y, Material mat)
            {
                if (v.Count == 0) return null;
                var go = new GameObject(name);
                go.transform.SetParent(parent.transform, false);
                // Vertices relative to the first one keep the floats small; the object sits there instead.
                Vector3 o0 = v[0];
                for (int i = 0; i < v.Count; i++) v[i] -= o0;
                go.transform.localPosition = new Vector3(o0.x, y, o0.z);
                var mesh = new Mesh
                {
                    name = ("marks_" + name).Replace(' ', '_'),
                    indexFormat = v.Count > 65535 ? IndexFormat.UInt32 : IndexFormat.UInt16
                };
                mesh.SetVertices(v);
                mesh.SetUVs(0, uv);
                mesh.SetColors(col);
                mesh.SetTriangles(tri, 0);
                mesh.RecalculateNormals();
                mesh.RecalculateTangents();
                mesh.RecalculateBounds();
                AssetDatabase.CreateAsset(mesh, $"{AssetRoot}/Meshes/{mesh.name}.asset");
                go.AddComponent<MeshFilter>().sharedMesh = mesh;
                go.AddComponent<MeshRenderer>().sharedMaterial = mat;
                return go;
            }
        }

        // A runway number 1-36 from a designator ("05L" -> 5), or from the heading when it has no digits.
        static int RunwayNumber(string ident, float headingDeg)
        {
            int n = 0, digits = 0;
            foreach (char ch in ident ?? "")
            {
                if (ch < '0' || ch > '9') break;
                n = n * 10 + (ch - '0'); digits++;
            }
            if (digits == 0 || n < 1 || n > 36)
            {
                n = Mathf.RoundToInt(Mathf.Repeat(headingDeg, 360f) / 10f);
                if (n == 0) n = 36;
            }
            return n;
        }

        // Markings for one runway from its low end le to its high end he: both ends' threshold bars, numbers,
        // touchdown and aiming-point bars, then the centreline dashes and the two edge lines.
        static void PaintRunway(Paint p, Vector2 le, Vector2 he, float W, string leIdent, string heIdent)
        {
            float L = (he - le).magnitude;
            if (L < 200f || W < 8f) return;
            var d = (he - le) / L;
            float hdg = Mathf.Atan2(d.x, d.y) * Mathf.Rad2Deg;
            float numbersEnd = PaintEnd(p, le, d, L, W, RunwayNumber(leIdent, hdg));
            PaintEnd(p, he, -d, L, W, RunwayNumber(heIdent, hdg + 180f));
            var r = new Vector2(d.y, -d.x);
            float lw = W >= 30f ? 0.9f : 0.6f;
            float edge = W / 2 - 1.2f;
            p.Line(le + r * edge, he + r * edge, lw, AtlasWhite);
            p.Line(le - r * edge, he - r * edge, lw, AtlasWhite);
            float cw = W >= 30f ? 0.9f : 0.45f;
            for (float a = numbersEnd + 20f; a + 30f <= L - numbersEnd - 20f; a += 50f)
                p.Quad(le + d * a - r * (cw / 2), d, 30f, cw, AtlasWhite.x, AtlasWhite.y, 0f, 0.75f);
        }

        // One runway end at T, f pointing into the runway. Returns how far in its numbers reach.
        static float PaintEnd(Paint p, Vector2 T, Vector2 f, float L, float W, int number)
        {
            var g = new Vector2(f.y, -f.x);            // right of the landing direction
            bool wide = W >= 30f;
            // Threshold bars, 6-36 m in, in two groups either side of the centreline.
            float bw = wide ? 1.8f : 1.2f;
            for (int i = 0; i < 8; i++)
            {
                float inner = bw + i * 2 * bw;
                if (inner + bw > W / 2 - 3f) break;
                foreach (float side in new[] { -1f, 1f })
                {
                    float t0 = side > 0 ? inner : -inner - bw;
                    p.Quad(T + f * 6f + g * t0, f, 30f, bw, AtlasBar.x, AtlasBar.y, 0.02f, 0.98f);
                }
            }
            // The number, read upright by a pilot landing over this end: two digits side by side.
            float h = Mathf.Clamp(W * 0.4f, 9f, 18f), dw = h / 3f, gap = h / 6f, s0 = 48f;
            for (int k = 0; k < 2; k++)
            {
                int digit = k == 0 ? number / 10 : number % 10;
                float px = DigitRightPx[digit] - DigitLeftPx[digit];
                float qw = dw * Mathf.Min(1f, px / DigitFullPx);
                float slot = k == 0 ? -dw - gap / 2 : gap / 2;
                float u0 = (DigitLeftPx[digit] - 3) / 2048f, u1 = (DigitRightPx[digit] + 4) / 2048f;
                float vTop = 1f - (DigitRowTopPx[digit] - 3) / 2048f, vBottom = 1f - (DigitRowTopPx[digit] + 280) / 2048f;
                p.Quad(T + f * s0 + g * (slot + (dw - qw) / 2), f, h, qw, u0, u1, vBottom, vTop);
            }
            float numbersEnd = s0 + h;
            // Touchdown-zone bars (150-900 m in) and the aiming point (300 m in), on runways long enough for them.
            float inner2 = W * 0.2f, tb = wide ? 1.8f : 1.2f;
            var zones = new[] { (150f, 3), (300f, 0), (450f, 2), (600f, 2), (750f, 1), (900f, 1) };
            foreach (var (s, n) in zones)
            {
                if (s + 45f > L / 2 - 30f) break;
                foreach (float side in new[] { -1f, 1f })
                {
                    if (n == 0)
                    {
                        if (L < 1200f) continue;
                        float aw = Mathf.Clamp(W * 0.2f, 4f, 9f);
                        float t0 = side > 0 ? inner2 : -inner2 - aw;
                        p.Quad(T + f * s + g * t0, f, 45f, aw, AtlasBlock.x, AtlasBlock.y, 0.02f, 0.98f);
                        continue;
                    }
                    if (L < 1500f) continue;
                    for (int i = 0; i < n; i++)
                    {
                        float a0 = inner2 + i * 2 * tb;
                        if (a0 + tb > W / 2 - 3f) break;
                        float t0 = side > 0 ? a0 : -a0 - tb;
                        p.Quad(T + f * s + g * t0, f, 22.5f, tb, AtlasBar.x, AtlasBar.y, 0.02f, 0.98f);
                    }
                }
            }
            return numbersEnd;
        }

        // One mesh of rectangles in an airfield's own frame (along the runway, across towards the apron),
        // tessellated so no triangle is too large for PhysX. render false: collider only.
        static GameObject AddSurface(GameObject parent, string name, Vector2 centre, Vector2 dir, Vector2 across,
                                     Rect2[] rects, float y, Material mat, Func<float, float, Vector2> uv, float cell,
                                     bool render, bool collider)
        {
            var verts = new List<Vector3>();
            var uvs = new List<Vector2>();
            var tris = new List<int>();
            // Front faces must point up: Unity's front face is clockwise seen from that side.
            bool flip = dir.x * across.y - dir.y * across.x > 0;
            foreach (var r in rects)
            {
                int na = Mathf.Max(1, Mathf.CeilToInt((r.a1 - r.a0) / cell));
                int nc = Mathf.Max(1, Mathf.CeilToInt((r.c1 - r.c0) / cell));
                int baseIdx = verts.Count;
                for (int i = 0; i <= na; i++)
                for (int j = 0; j <= nc; j++)
                {
                    float al = Mathf.Lerp(r.a0, r.a1, (float)i / na), ac = Mathf.Lerp(r.c0, r.c1, (float)j / nc);
                    Vector2 q = dir * al + across * ac;
                    verts.Add(new Vector3(q.x, 0f, q.y));
                    uvs.Add(uv != null ? uv(al, ac) : Vector2.zero);
                }
                int row = nc + 1;
                for (int i = 0; i < na; i++)
                for (int j = 0; j < nc; j++)
                {
                    int v = baseIdx + i * row + j;
                    if (!flip) { tris.Add(v); tris.Add(v + row); tris.Add(v + 1); tris.Add(v + 1); tris.Add(v + row); tris.Add(v + row + 1); }
                    else { tris.Add(v); tris.Add(v + 1); tris.Add(v + row); tris.Add(v + 1); tris.Add(v + row + 1); tris.Add(v + row); }
                }
            }
            if (verts.Count == 0) return null;
            var go = new GameObject(name);
            go.transform.SetParent(parent.transform, false);
            go.transform.localPosition = new Vector3(centre.x, y, centre.y);
            var mesh = new Mesh
            {
                name = (parent.name + "_" + name).Replace(' ', '_'),
                indexFormat = verts.Count > 65535 ? IndexFormat.UInt32 : IndexFormat.UInt16
            };
            mesh.SetVertices(verts);
            mesh.SetUVs(0, uvs);
            mesh.SetTriangles(tris, 0);
            mesh.RecalculateNormals();
            mesh.RecalculateTangents();
            mesh.RecalculateBounds();
            AssetDatabase.CreateAsset(mesh, $"{AssetRoot}/Meshes/{mesh.name}.asset");
            if (render)
            {
                go.AddComponent<MeshFilter>().sharedMesh = mesh;
                go.AddComponent<MeshRenderer>().sharedMaterial = mat;
            }
            if (collider) go.AddComponent<MeshCollider>().sharedMesh = mesh;
            return go;
        }

        // Minimal .npy reader: little-endian float32, C order, 2-D.
        static float[,] ReadNpy(string path)
        {
            using var f = File.OpenRead(path);
            using var r = new BinaryReader(f);
            byte[] magic = r.ReadBytes(6);
            if (magic[0] != 0x93 || magic[1] != (byte)'N') throw new InvalidDataException("not .npy");
            byte major = r.ReadByte(); r.ReadByte();
            int hlen = major == 1 ? r.ReadUInt16() : (int)r.ReadUInt32();
            string header = System.Text.Encoding.ASCII.GetString(r.ReadBytes(hlen));
            if (!header.Contains("'<f4'") || header.Contains("'fortran_order': True")) throw new InvalidDataException(header);
            string shape = header.Substring(header.IndexOf("'shape': (") + 10);
            shape = shape.Substring(0, shape.IndexOf(')'));
            int[] dims = shape.Split(',').Where(s => s.Trim().Length > 0).Select(s => int.Parse(s.Trim())).ToArray();
            var a = new float[dims[0], dims[1]];
            byte[] row = new byte[dims[1] * 4];
            for (int i = 0; i < dims[0]; i++)
            {
                int got = 0;
                while (got < row.Length) got += f.Read(row, got, row.Length - got);
                Buffer.BlockCopy(row, 0, a, i * row.Length, row.Length);
            }
            return a;
        }
    }
}
