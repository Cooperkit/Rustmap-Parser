using System;
using System.Collections.Generic;
using System.Linq;
using Oxide.Core;
using UnityEngine;

namespace Oxide.Plugins
{
    [Info("MonumentDetailsProbe", "rust-map-parser", "1.1.0")]
    [Description("Exports runtime monument interactables, puzzles, loot spawners, and radiation for parser validation.")]
    public class MonumentDetailsProbe : RustPlugin
    {
        private const string DataFileName = "MonumentDetailsProbe";

        private void OnServerInitialized()
        {
            timer.Once(5f, Export);
        }

        [ConsoleCommand("monumentdetails.export")]
        private void ExportCommand(ConsoleSystem.Arg arg)
        {
            if (arg.Connection != null && (arg.Player() == null || !arg.Player().IsAdmin))
            {
                arg.ReplyWith("Admin access required.");
                return;
            }
            Export();
            arg.ReplyWith("Monument runtime details exported to oxide/data/MonumentDetailsProbe.json");
        }

        private void Export()
        {
            var monuments = TerrainMeta.Path == null
                ? new List<MonumentInfo>()
                : TerrainMeta.Path.Monuments.Where(value => value != null).ToList();
            var entities = BaseNetworkable.serverEntities
                .Select(value => value as BaseEntity)
                .Where(value => value != null && !value.IsDestroyed)
                .ToList();

            var monumentRows = monuments.Select((monument, index) => new Dictionary<string, object>
            {
                ["id"] = $"monument-{index + 1:000}",
                ["root_name"] = monument.transform.root.name,
                ["object_name"] = monument.name,
                ["position"] = Vector(monument.transform.position),
                ["rotation_euler"] = Vector(monument.transform.eulerAngles),
                ["bounds_center"] = Vector(monument.Bounds.center),
                ["bounds_extents"] = Vector(monument.Bounds.extents),
                ["is_safe_zone"] = monument.IsSafeZone,
                ["tier"] = monument.Tier.ToString(),
            }).ToList();

            var monumentIds = monuments.Select((monument, index) => new MonumentRef
            {
                Monument = monument,
                Id = $"monument-{index + 1:000}"
            }).ToList();

            var interactables = new List<Dictionary<string, object>>();
            foreach (var entity in entities)
            {
                Dictionary<string, object> properties;
                var kind = InteractableType(entity, out properties);
                if (kind == null)
                    continue;
                var monument = ClosestMonument(entity.transform.position, monumentIds);
                interactables.Add(new Dictionary<string, object>
                {
                    ["type"] = kind,
                    ["component_type"] = entity.GetType().Name,
                    ["entity_id"] = EntityId(entity),
                    ["prefab_name"] = entity.PrefabName,
                    ["short_prefab_name"] = entity.ShortPrefabName,
                    ["position"] = Vector(entity.transform.position),
                    ["rotation_euler"] = Vector(entity.transform.eulerAngles),
                    ["monument_id"] = monument == null ? null : monument.Id,
                    ["monument_local_position"] = monument == null
                        ? null
                        : Vector(monument.Monument.transform.InverseTransformPoint(entity.transform.position)),
                    ["properties"] = properties,
                });
            }

            var ioRows = new List<Dictionary<string, object>>();
            foreach (var entity in entities.OfType<IOEntity>())
            {
                var monument = ClosestMonument(entity.transform.position, monumentIds);
                if (monument == null)
                    continue;
                var outputs = new List<Dictionary<string, object>>();
                for (var index = 0; index < entity.outputs.Length; index++)
                {
                    var slot = entity.outputs[index];
                    var target = slot.connectedTo.Get(true);
                    if (target == null)
                        continue;
                    outputs.Add(new Dictionary<string, object>
                    {
                        ["output_slot"] = index,
                        ["output_name"] = slot.niceName,
                        ["target_entity_id"] = EntityId(target),
                        ["input_slot"] = slot.connectedToSlot,
                    });
                }
                var properties = new Dictionary<string, object>();
                var reader = entity as CardReader;
                if (reader != null)
                    properties["access_level"] = reader.accessLevel;
                ioRows.Add(new Dictionary<string, object>
                {
                    ["entity_id"] = EntityId(entity),
                    ["component_type"] = entity.GetType().Name,
                    ["prefab_name"] = entity.PrefabName,
                    ["object_name"] = entity.name,
                    ["position"] = Vector(entity.transform.position),
                    ["rotation_euler"] = Vector(entity.transform.eulerAngles),
                    ["monument_id"] = monument.Id,
                    ["monument_local_position"] = Vector(
                        monument.Monument.transform.InverseTransformPoint(entity.transform.position)
                    ),
                    ["properties"] = properties,
                    ["outputs"] = outputs,
                });
            }

            var puzzleRows = new List<Dictionary<string, object>>();
            foreach (var reset in UnityEngine.Object.FindObjectsOfType<PuzzleReset>())
            {
                var monument = ClosestMonument(reset.transform.position, monumentIds);
                if (monument == null)
                    continue;
                var attached = reset.GetComponent<IOEntity>();
                puzzleRows.Add(new Dictionary<string, object>
                {
                    ["object_name"] = reset.name,
                    ["position"] = Vector(reset.transform.position),
                    ["monument_id"] = monument.Id,
                    ["monument_local_position"] = Vector(
                        monument.Monument.transform.InverseTransformPoint(reset.transform.position)
                    ),
                    ["attached_io_entity_id"] = attached == null ? null : EntityId(attached),
                    ["reset_entity_ids"] = (reset.resetEnts ?? Array.Empty<IOEntity>())
                        .Where(value => value != null).Select(EntityId).ToList(),
                    ["reset_positions"] = (reset.resetPositions ?? Array.Empty<Vector3>())
                        .Select(value => Vector(reset.transform.TransformPoint(value))).ToList(),
                });
            }

            var lootSpawnRows = new List<Dictionary<string, object>>();
            foreach (var group in UnityEngine.Object.FindObjectsOfType<SpawnGroup>())
            {
                var monument = monumentIds.FirstOrDefault(
                    value => value.Monument == group.Monument
                );
                if (monument == null)
                    continue;
                var points = group.spawnPoints ?? group.GetComponentsInChildren<BaseSpawnPoint>();
                var variants = (group.prefabs ?? new List<SpawnGroup.SpawnEntry>())
                    .Where(value => value != null && value.prefab != null)
                    .Select(value => new Dictionary<string, object>
                    {
                        ["prefab_path"] = value.prefab.resourcePath,
                        ["weight"] = value.weight,
                    }).ToList();
                if (!variants.Any(value => IsLootPrefab(value["prefab_path"] as string)))
                    continue;
                lootSpawnRows.Add(new Dictionary<string, object>
                {
                    ["object_name"] = group.name,
                    ["monument_id"] = monument.Id,
                    ["current_population"] = group.currentPopulation,
                    ["max_population"] = group.maxPopulation,
                    ["spawn_per_tick_min"] = group.numToSpawnPerTickMin,
                    ["spawn_per_tick_max"] = group.numToSpawnPerTickMax,
                    ["respawn_seconds_min"] = group.respawnDelayMin,
                    ["respawn_seconds_max"] = group.respawnDelayMax,
                    ["variants"] = variants,
                    ["spawn_points"] = points.Where(value => value != null)
                        .Select(value => new Dictionary<string, object>
                        {
                            ["component_type"] = value.GetType().Name,
                            ["position"] = Vector(value.transform.position),
                            ["monument_local_position"] = Vector(
                                monument.Monument.transform.InverseTransformPoint(
                                    value.transform.position
                                )
                            ),
                            ["radius"] = value is RadialSpawnPoint
                                ? ((RadialSpawnPoint)value).radius : 0f,
                        }).ToList(),
                });
            }

            var radiationRows = new List<Dictionary<string, object>>();
            foreach (var trigger in UnityEngine.Object.FindObjectsOfType<TriggerRadiation>())
            {
                var owner = trigger.GetComponentInParent<MonumentInfo>();
                var monument = monumentIds.FirstOrDefault(value => value.Monument == owner);
                if (monument == null)
                    continue;
                var sphere = trigger.GetComponent<SphereCollider>();
                var box = trigger.GetComponent<BoxCollider>();
                var amount = trigger.RadiationAmountOverride > 0f
                    ? trigger.RadiationAmountOverride
                    : Radiation.GetRadiation(trigger.radiationTier);
                var row = new Dictionary<string, object>
                {
                    ["object_name"] = trigger.name,
                    ["monument_id"] = monument.Id,
                    ["position"] = Vector(trigger.transform.position),
                    ["monument_local_position"] = Vector(
                        monument.Monument.transform.InverseTransformPoint(
                            trigger.transform.position
                        )
                    ),
                    ["rotation_euler"] = Vector(trigger.transform.eulerAngles),
                    ["radiation_tier"] = trigger.radiationTier.ToString(),
                    ["radiation_amount"] = amount,
                    ["dynamic"] = trigger.GetComponentInParent<RadiationSphere>() != null,
                    ["bypass_armor"] = trigger.BypassArmor,
                    ["falloff"] = trigger.falloff,
                    ["increase_near_center"] = trigger.IncreaseDamageNearCenter,
                };
                if (sphere != null)
                {
                    var scale = trigger.transform.localScale;
                    row["shape"] = "sphere";
                    row["radius"] = trigger.DontScaleRadiationSize
                        ? sphere.radius
                        : sphere.radius * Mathf.Max(scale.x, Mathf.Max(scale.y, scale.z));
                }
                else if (box != null)
                {
                    row["shape"] = "box";
                    row["position"] = Vector(trigger.transform.TransformPoint(box.center));
                    row["size"] = Vector(Vector3.Scale(box.size, trigger.transform.lossyScale));
                }
                else
                {
                    continue;
                }
                radiationRows.Add(row);
            }

            Interface.Oxide.DataFileSystem.WriteObject(DataFileName, new Dictionary<string, object>
            {
                ["schema_version"] = 2,
                ["generated_utc"] = DateTime.UtcNow.ToString("O"),
                ["world_size"] = World.Size,
                ["monuments"] = monumentRows,
                ["interactables"] = interactables,
                ["io_entities"] = ioRows,
                ["puzzle_resets"] = puzzleRows,
                ["loot_spawn_groups"] = lootSpawnRows,
                ["radiation_zones"] = radiationRows,
                ["notes"] = "Runtime validation data. The parser does not require or consume this file.",
            });
            Puts($"Exported {monumentRows.Count} monuments, {interactables.Count} interactables, " +
                 $"{ioRows.Count} monument IO entities, {puzzleRows.Count} puzzle resets, " +
                 $"{lootSpawnRows.Count} loot groups, and {radiationRows.Count} radiation zones.");
        }

        private static string InteractableType(BaseEntity entity,
                                                out Dictionary<string, object> properties)
        {
            properties = new Dictionary<string, object>();
            if (entity is Recycler) return "recycler";
            if (entity is ResearchTable) return "research_table";
            if (entity is RepairBench) return "repair_bench";
            if (entity is MixingTable) return "mixing_table";
            var workbench = entity as Workbench;
            if (workbench != null)
            {
                properties["level"] = workbench.Workbenchlevel;
                return "workbench";
            }
            if (entity is NPCVendingMachine || entity is InvisibleVendingMachine)
                return "vending_machine";
            if (entity is Marketplace) return "marketplace";
            var oven = entity as BaseOven;
            if (oven != null && (oven.IndustrialMode == BaseOven.IndustrialSlotMode.OilRefinery ||
                                 oven.ShortPrefabName.IndexOf("refinery", StringComparison.OrdinalIgnoreCase) >= 0))
                return "oil_refinery";
            return null;
        }

        private static bool IsLootPrefab(string path)
        {
            if (string.IsNullOrEmpty(path))
                return false;
            var name = path.ToLowerInvariant();
            return name.Contains("barrel") || name.Contains("crate") ||
                name.Contains("diesel_collectable");
        }

        private static MonumentRef ClosestMonument(Vector3 position, List<MonumentRef> monuments)
        {
            var inside = monuments.Where(value => value.Monument.IsInBounds(position)).ToList();
            if (inside.Count > 0)
                return inside.OrderBy(value => value.Monument.SqrDistance(position)).First();
            var nearest = monuments.OrderBy(value => value.Monument.SqrDistance(position)).FirstOrDefault();
            if (nearest == null || nearest.Monument.Distance(position) > 700f)
                return null;
            return nearest;
        }

        private static string EntityId(BaseEntity entity)
        {
            return entity.net == null ? $"instance:{entity.GetInstanceID()}" : entity.net.ID.Value.ToString();
        }

        private static Dictionary<string, float> Vector(Vector3 value)
        {
            return new Dictionary<string, float>
            {
                ["x"] = value.x,
                ["y"] = value.y,
                ["z"] = value.z,
            };
        }

        private class MonumentRef
        {
            public MonumentInfo Monument;
            public string Id;
        }
    }
}
