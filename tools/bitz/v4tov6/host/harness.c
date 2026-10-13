/*
 * Host harness: dry-runs a v6 settings backup through v6's OWN code.
 *
 * Compiled together with (unmodified, from the v6 tree):
 *   config_backup_core.c   cb_json_find/members: the header + part scanner
 *   config_backup_codec.c  cb_unredact: the redacted blank-secret rule
 *   settings_manager_fields.c  field table -> JSON Schema
 *   settings_manager_schema.c  sm_schema_validate / collect_defaults
 *   every emitted component's *_settings.c (its real FIELDS table,
 *   version, on_validate and on_migrate), through gen_regs.c
 *
 * It mirrors config_backup_restore.c (read_header, index_doc v1) and
 * config_backup_apply.c settings_walk(dry=true) + settings_manager_restore
 * (dry_run): migrate when older, fill defaults, validate, on_validate.
 * What it cannot run is settings_manager_get() (the device's stored values):
 * the stored document is the schema defaults (a fresh device) or the
 * per-component objects of the optional <stored.json>.
 *
 *   harness <backup.json> [stored.json] [booted 0|1]
 */
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "settings_manager.h"
#include "settings_manager_private.h"
#include "config_backup_private.h"

typedef struct
{
    const char *name;
    const settings_descriptor_t *(*get)(void);
} hv_reg_t;

extern const hv_reg_t HV_REGS[];
extern void hv_wifi_set_booted(bool booted);

static const settings_descriptor_t *s_last;

esp_err_t settings_manager_register(const settings_descriptor_t *desc)
{
    s_last = desc;
    return ESP_OK;
}

const settings_descriptor_t *hv_take(void)
{
    const settings_descriptor_t *d = s_last;

    s_last = NULL;
    return d;
}

const char *esp_err_to_name(esp_err_t e)
{
    static char buf[16];

    snprintf(buf, sizeof(buf), "0x%x", e);
    return buf;
}

static char *slurp(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");

    if (f == NULL)
    {
        return NULL;
    }

    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *b = malloc((size_t)n + 1);

    if (b == NULL || fread(b, 1, (size_t)n, f) != (size_t)n)
    {
        fclose(f);
        free(b);
        return NULL;
    }

    fclose(f);
    b[n] = '\0';
    *len = (size_t)n;
    return b;
}

static const settings_descriptor_t *find_desc(const char *name)
{
    for (int i = 0; HV_REGS[i].name != NULL; i++)
    {
        if (strcmp(HV_REGS[i].name, name) == 0)
        {
            return HV_REGS[i].get();
        }
    }

    return NULL;
}

/* register()'s schema derivation + sm_build_defaults() */
static char *schema_of(const settings_descriptor_t *d)
{
    if (d->schema != NULL)
    {
        return strdup(d->schema);
    }

    char *s = NULL;

    if (sm_schema_from_fields(d->fields, d->field_count, &s) != ESP_OK)
    {
        return NULL;
    }

    return s;
}

static cJSON *defaults_of(const settings_descriptor_t *d, const char *schema)
{
    if (d->defaults_json != NULL)
    {
        return cJSON_Parse(d->defaults_json);
    }

    cJSON *defs = NULL;

    if (sm_schema_collect_defaults(schema, &defs) != ESP_OK || defs == NULL)
    {
        defs = cJSON_CreateObject();
    }

    return defs;
}

static void fill_missing(cJSON *dst, const cJSON *src)
{
    const cJSON *it = NULL;

    cJSON_ArrayForEach(it, src)
    {
        if (cJSON_GetObjectItemCaseSensitive(dst, it->string) == NULL)
        {
            cJSON_AddItemToObject(dst, it->string, cJSON_Duplicate(it, true));
        }
    }
}

int main(int argc, char **argv)
{
    if (argc < 2)
    {
        fprintf(stderr, "usage: %s backup.json [stored.json] [booted 0|1]\n",
                argv[0]);
        return 2;
    }

    size_t len = 0;
    char  *buf = slurp(argv[1], &len);

    if (buf == NULL)
    {
        fprintf(stderr, "cannot read %s\n", argv[1]);
        return 2;
    }

    cJSON *stored_all = NULL;

    if (argc >= 3 && strcmp(argv[2], "-") != 0)
    {
        size_t slen = 0;
        char  *s = slurp(argv[2], &slen);

        stored_all = s ? cJSON_Parse(s) : NULL;
        free(s);
    }

    hv_wifi_set_booted(argc >= 4 ? atoi(argv[3]) != 0 : true);

    /* read_header() */
    cb_member_t m;

    if (!cb_json_valid(buf, len) || !cb_json_find(buf, len, "wican_backup", &m))
    {
        printf("REFUSED: not a backup document\n");
        return 1;
    }

    long ver = cb_json_long(&m);
    bool redacted = cb_json_find(buf, len, "redacted", &m) && cb_json_bool(&m);

    if (ver != 1)
    {
        printf("REFUSED: harness handles document version 1 only (got %ld)\n", ver);
        return 1;
    }

    /* index_doc() v1 */
    if (!cb_json_find(buf, len, "components", &m))
    {
        printf("REFUSED: no components\n");
        return 1;
    }

    cJSON *root = cJSON_ParseWithLength(m.val, m.val_len);

    if (!cJSON_IsObject(root))
    {
        printf("REFUSED: the settings part is not an object\n");
        return 1;
    }

    int    bad = 0, known = 0, skipped = 0;
    cJSON *comp = NULL;

    cJSON_ArrayForEach(comp, root)
    {
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(comp, "version");
        cJSON       *data = cJSON_GetObjectItemCaseSensitive(comp, "data");

        if (!cJSON_IsNumber(v) || !cJSON_IsObject(data))
        {
            printf("  %-18s ERR needs {version, data}\n", comp->string);
            bad++;
            break;
        }

        const settings_descriptor_t *d = find_desc(comp->string);

        if (d == NULL)
        {
            printf("  %-18s SKIPPED (no such component in this firmware)\n",
                   comp->string);
            skipped++;
            continue;
        }

        char  *schema = schema_of(d);
        cJSON *defs = defaults_of(d, schema);

        /* merge_stored(): the redacted blank-secret rule against what the
           device stores (here: stored.json's object, else the defaults) */
        const cJSON *stored = stored_all
            ? cJSON_GetObjectItemCaseSensitive(stored_all, comp->string) : NULL;
        cJSON *st = stored ? cJSON_Duplicate(stored, true)
                           : cJSON_Duplicate(defs, true);

        if (stored)
        {
            fill_missing(st, defs);
        }

        cJSON *cand = cJSON_Duplicate(data, true);

        if (redacted)
        {
            cb_unredact(cand, st, comp->string);
        }

        char      err[160] = "";
        esp_err_t r = ESP_OK;
        uint32_t  bver = (uint32_t)v->valuedouble;

        if (bver > d->version)
        {
            snprintf(err, sizeof(err), "backup is v%u but this firmware has v%u",
                     (unsigned)bver, (unsigned)d->version);
            r = ESP_ERR_INVALID_VERSION;
        }
        else if (bver < d->version &&
                 (d->on_migrate == NULL || d->on_migrate(bver, cand) != ESP_OK))
        {
            snprintf(err, sizeof(err), "no migration path");
            r = ESP_ERR_INVALID_ARG;
        }

        if (r == ESP_OK)
        {
            fill_missing(cand, defs);
            r = sm_schema_validate(schema, cand, NULL, err, sizeof(err));

            if (r == ESP_OK && d->on_validate != NULL)
            {
                r = d->on_validate(cand, err, sizeof(err));
            }
        }

        if (r == ESP_OK)
        {
            printf("  %-18s OK   v%u (%d keys after defaults)\n", comp->string,
                   (unsigned)d->version, cJSON_GetArraySize(cand));
            known++;
        }
        else
        {
            printf("  %-18s ERR  %s\n", comp->string, err);
            bad++;
        }

        cJSON_Delete(cand);
        cJSON_Delete(st);
        cJSON_Delete(defs);
        free(schema);

        if (bad)
        {
            break; /* settings_walk stops at the first error */
        }
    }

    printf("%s: groups=%d skipped=%d redacted=%s\n",
           bad ? "REFUSED" : "ACCEPTED", known, skipped,
           redacted ? "true" : "false");
    cJSON_Delete(root);
    cJSON_Delete(stored_all);
    free(buf);
    return bad ? 1 : 0;
}
