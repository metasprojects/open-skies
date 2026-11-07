// Builds a NOCustomMaps map bundle (.nomap) from a NO-Earth build folder.
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
            terrainMacroColor, splatGrass, splatRock, splatLush, splatFields, airbases, mapImage, seaLanes, roadNetwork; }
        [Serializable] class Manifest { public string mapId; public float mapSizeX; public Assets assets; }
        [Serializable] class Runway { public float startX, startZ, endX, endZ, width; }
        [Serializable] class Airbase { public string uniqueName; public float x, y, z, heading, runwayLength, runwayWidth;
            public Runway[] extraRunways; }
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

        static void BuildAirfields(GameObject root, string buildDir)
        {
            string path = Path.Combine(buildDir, "airbases.json");
            if (!File.Exists(path)) return;
            var list = JsonUtility.FromJson<AirbaseList>("{\"items\":" + File.ReadAllText(path) + "}");
            // A placeholder: NOCustomMaps swaps any material named __BORROW__Runway for the game's runway at load.
            var shader = Shader.Find("Unlit/Color") ?? Shader.Find("Standard") ?? Shader.Find("Sprites/Default");
            var runwayMat = new Material(shader) { name = "__BORROW__Runway" };
            AssetDatabase.CreateAsset(runwayMat, $"{AssetRoot}/__BORROW__Runway.mat");
            var fields = new GameObject("Airfields");
            fields.transform.SetParent(root.transform, false);
            foreach (var ab in list.items)
            {
                var g = new GameObject(ab.uniqueName);
                g.transform.SetParent(fields.transform, false);
                float hr = ab.heading * Mathf.Deg2Rad;
                Vector2 d = new Vector2(Mathf.Sin(hr), Mathf.Cos(hr)) * (ab.runwayLength / 2f);
                AddStrip(g, "runway", new Vector2(ab.x - d.x, ab.z - d.y), new Vector2(ab.x + d.x, ab.z + d.y), ab.runwayWidth, ab.y, runwayMat);
                int k = 1;
                foreach (var r in ab.extraRunways ?? Array.Empty<Runway>())
                    AddStrip(g, $"runway_{k++}", new Vector2(r.startX, r.startZ), new Vector2(r.endX, r.endZ), r.width, ab.y, runwayMat);
            }
        }

        static void AddStrip(GameObject parent, string name, Vector2 a, Vector2 b, float width, float y, Material mat)
        {
            Vector2 dir = (b - a).normalized;
            Vector2 side = new Vector2(dir.y, -dir.x) * (width / 2f);
            Vector2 c = (a + b) / 2f;
            var go = new GameObject(name);
            go.transform.SetParent(parent.transform, false);
            go.transform.localPosition = new Vector3(c.x, y + 0.05f, c.y);
            var mesh = new Mesh { name = parent.name + "_" + name };
            Vector3 P(Vector2 v) => new Vector3(v.x - c.x, 0f, v.y - c.y);
            // Tessellated: PhysX refuses to cook a collider from triangles kilometres long.
            float len = (b - a).magnitude;
            int along = Mathf.Max(1, Mathf.CeilToInt(len / 25f));
            int across = Mathf.Max(1, Mathf.CeilToInt(width / 25f));
            var verts = new List<Vector3>();
            var uvs = new List<Vector2>();
            for (int i = 0; i <= along; i++)
            for (int k = 0; k <= across; k++)
            {
                float t = (float)i / along, s = (float)k / across;
                verts.Add(P(Vector2.Lerp(a, b, t) + side * (2f * s - 1f)));
                uvs.Add(new Vector2(s, t * len / width));
            }
            var tris = new List<int>();
            int row = across + 1;
            for (int i = 0; i < along; i++)
            for (int k = 0; k < across; k++)
            {
                int v = i * row + k;
                tris.Add(v); tris.Add(v + row); tris.Add(v + 1);
                tris.Add(v + 1); tris.Add(v + row); tris.Add(v + row + 1);
            }
            mesh.SetVertices(verts);
            mesh.SetUVs(0, uvs);
            mesh.SetTriangles(tris, 0);
            mesh.RecalculateNormals();
            mesh.RecalculateBounds();
            AssetDatabase.CreateAsset(mesh, $"{AssetRoot}/Meshes/{mesh.name.Replace(' ', '_')}.asset");
            go.AddComponent<MeshFilter>().sharedMesh = mesh;
            go.AddComponent<MeshRenderer>().sharedMaterial = mat;
            go.AddComponent<MeshCollider>().sharedMesh = mesh;
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
