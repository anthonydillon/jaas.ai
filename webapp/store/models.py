import collections
import gfm
import os
import re

from jujubundlelib import references
from theblues.charmstore import CharmStore, DEFAULT_INCLUDES
from theblues.errors import EntityNotFound, ServerError
from theblues.terms import Terms


cs = CharmStore()
terms = Terms("https://api.jujucharms.com/terms/")

SEARCH_LIMIT = 400


def search_entities(
    query,
    entity_type=None,
    tags=None,
    sort=None,
    series=None,
    owner=None,
    promulgated_only=None,
):
    includes = [
        "charm-metadata",
        "bundle-metadata",
        "owner",
        "bundle-unit-count",
        "promulgated",
        "supported-series",
    ]
    try:
        entities = cs.search(
            query,
            doc_type=entity_type,
            includes=includes,
            limit=SEARCH_LIMIT,
            owner=owner,
            promulgated_only=False,
            series=series,
            sort=sort,
            tags=tags,
        )
        results = {"community": [], "recommended": []}
        for entity in _parse_list(entities):
            group = "recommended" if entity["promulgated"] else "community"
            results[group].append(entity)
        return results
    except EntityNotFound:
        return None


def get_reference(entity):
    """From a string, see if we can make an entity reference out of it, using
        all possible methods.
        :param string: a string to turn into a reference.
        :returns: A reference or None.
    """
    reference = None
    try:
        reference = references.Reference.from_jujucharms_url(entity)
    except ValueError:
        pass
    try:
        reference = references.Reference.from_string(entity)
    except ValueError:
        pass
    try:
        reference = references.Reference.from_jujucharms_url(entity)
    except ValueError:
        pass
    try:
        reference = references.Reference.from_fully_qualified_url(entity)
    except ValueError:
        pass
    return reference


def _group_status(entities):
    results = {"community": [], "recommended": []}
    for entity in _parse_list(entities):
        group = "recommended" if entity.get("promulgated") else "community"
        results[group].append(entity)
    return results


def fetch_provides(id):
    results = cs.fetch_interfaces(id, "provides")
    return _group_status(list(results)[2])


def fetch_requires(id):
    results = cs.fetch_interfaces(id, "requires")
    return _group_status(list(results)[2])


def get_user_entities(username):
    includes = [
        "charm-metadata",
        "bundle-metadata",
        "extra-info",
        "owner",
        "bundle-unit-count",
        "bundle-machine-count",
        "supported-series",
        "published",
    ]
    try:
        entities = cs.list(includes=includes, owner=username)
        parsed = _parse_list(entities)
        return _group_entities(parsed)
    except EntityNotFound:
        return None


def get_charm_or_bundle(reference):
    includes = DEFAULT_INCLUDES[:]
    includes.append("revision-info")
    try:
        entity_data = cs.entity(reference, True, includes=includes)
        return _parse_charm_or_bundle(entity_data, include_files=True)
    except EntityNotFound:
        return None


def get_terms(name, revision=None):
    try:
        terms_data = terms.get_terms(name, revision)
        return terms_data.content
    except ServerError:
        return None


def _parse_list(entities):
    return [_parse_charm_or_bundle(entity) for entity in entities]


def _group_entities(entities):
    groups = {"bundles": [], "charms": []}
    [
        groups["charms" if entity["is_charm"] else "bundles"].append(entity)
        for entity in entities
    ]
    return groups


def _parse_charm_or_bundle(entity_data, include_files=False):
    meta = entity_data.get("Meta", None)
    is_charm = meta.get("charm-metadata", False)
    if is_charm:
        return _parse_charm_data(entity_data, include_files)
    else:
        return _parse_bundle_data(entity_data, include_files)


def _parse_bundle_data(bundle_data, include_files=False):
    bundle_id = bundle_data["Id"]
    ref = references.Reference.from_string(bundle_id)
    name = ref.name
    meta = bundle_data["Meta"]
    bundle_metadata = meta["bundle-metadata"]
    revision_list = meta.get("revision-info", {}).get("Revisions")
    (
        _,
        _,
        supported,
        supported_price,
        supported_description,
    ) = _extract_from_extrainfo(meta, ref)
    description = bundle_metadata.get("Description")
    files = None
    readme = None
    if include_files:
        files = _get_entity_files(ref, meta.get("manifest"))
        readme = _render_markdown(cs.entity_readme_content(bundle_id))
    return {
        "archive_url": cs.archive_url(ref),
        "bundle_data": bundle_data,
        "bundle_visulisation": getBundleVisualization(ref),
        "card_id": ref.path(),
        "channels": meta.get("published", {}).get("Info"),
        # Some bundles don't have descriptions, so first check that the
        # description exists before trying to render the Markdown.
        "description": _render_markdown(description) if description else None,
        "display_name": _get_display_name(name),
        "files": files,
        "id": bundle_data.get("Id"),
        "is_charm": False,
        "latest_revision": _get_latest_revision(revision_list),
        "owner": meta.get("owner", {}).get("User"),
        "promulgated": meta.get("promulgated", {}).get("Promulgated"),
        "readme": readme,
        "revision_number": ref.revision,
        # The series is an array to match the charm data.
        "series": [bundle_metadata.get("Series")],
        "supported": supported,
        "supported_price": supported_price,
        "supported_description": supported_description
        and _render_markdown(supported_description),
        "services": _parseBundleServices(bundle_metadata["applications"]),
        "tags": bundle_metadata.get("Tags"),
        "units": meta.get("bundle-unit-count", {}).get("Count", ""),
        "url": ref.jujucharms_id(),
    }


def _get_latest_revision(revision_list):
    latest_revision = None
    if revision_list:
        ref = references.Reference.from_string(revision_list[0])
        latest_revision = {
            "id": ref.revision,
            "full_id": revision_list[0],
            "url": ref.jujucharms_id(),
        }
    return latest_revision


def _parseBundleServices(services):
    for k, v in services.items():
        ref = references.Reference.from_string(v["Charm"])
        v["Charm"] = ref.path()
        v["icon"] = cs.charm_icon_url(v["Charm"])
        v["url"] = ref.jujucharms_id()
        v["display_name"] = _get_display_name(k)

    return services


def _parse_charm_data(charm_data, include_files=False):
    charm_id = charm_data["Id"]
    ref = references.Reference.from_string(charm_id)
    meta = charm_data.get("Meta", None)
    charm_metadata = meta["charm-metadata"]
    (
        bzr_url,
        revisions,
        supported,
        supported_price,
        supported_description,
    ) = _extract_from_extrainfo(meta, ref)
    bugs_url, homepage = _extract_from_commoninfo(meta)
    name = charm_metadata["Name"]
    revision_list = meta.get("revision-info", {}).get("Revisions")
    description = charm_metadata.get("Description")
    files = None
    readme = None
    if include_files:
        files = _get_entity_files(ref, meta.get("manifest"))
        readme = _render_markdown(cs.entity_readme_content(charm_id))
    return {
        "archive_url": cs.archive_url(ref),
        "bugs_url": bugs_url,
        "bzr_url": bzr_url,
        "card_id": ref.path(),
        "channels": meta.get("published", {}).get("Info"),
        "charm_data": charm_data,
        # Some charms don't have descriptions, so first check that the
        # description exists before trying to render the Markdown.
        "description": _render_markdown(description) if description else None,
        "display_name": _get_display_name(name),
        "files": files,
        "homepage": homepage,
        "icon": cs.charm_icon_url(charm_id),
        "id": charm_id,
        "latest_revision": _get_latest_revision(revision_list),
        "options": meta.get("charm-config", {}).get("Options"),
        "owner": meta.get("owner", {}).get("User"),
        "promulgated": meta.get("promulgated", {}).get("Promulgated"),
        "provides": charm_metadata.get("Provides"),
        "readme": readme,
        "requires": charm_metadata.get("Requires"),
        "resources": _extract_resources(ref, meta.get("resources", {})),
        "revision_list": revision_list,
        "revision_number": ref.revision,
        "revisions": revisions,
        "series": meta.get("supported-series", {}).get("SupportedSeries"),
        "supported": supported,
        "supported_price": supported_price,
        "supported_description": supported_description
        and _render_markdown(supported_description),
        # Some charms do not have tags, so fall back to categories if they
        # exist (mostly on older charms).
        "is_charm": True,
        "tags": charm_metadata.get("Tags") or charm_metadata.get("Categories"),
        "term_ids": _parse_term_ids(meta.get("terms")),
        "url": ref.jujucharms_id(),
    }


def _parse_term_ids(term_ids):
    """Extract the term names and revisions.
        :param term_ids: A list of term ids.
        :returns: a collection of term ids, names and revisions.
    """
    if term_ids is None:
        return None
    terms = []
    for term in term_ids:
        parts = term.split("/")
        terms.append(
            {
                "id": term,
                "name": parts[0],
                "revision": int(parts[1]) if len(parts) == 2 else None,
            }
        )
    return terms


def _get_entity_files(ref, manifest=None):
    try:
        files = cs.files(ref, manifest=manifest) or {}
        files = collections.OrderedDict(sorted(files.items()))
    except EntityNotFound:
        files = {}
    return files


def getBundleVisualization(ref, fetch=False):
    """Get the url for the bundle visualization, or the actual svg.
        :param ref The reference of the bundle to get the visualization for.
        :param fetch Whether or not to get the actual svg.
        :returns the URL or the SVG for the bundle visualisation
    """
    if not fetch:
        return cs.bundle_visualization_url(ref)
    try:
        return cs.bundle_visualization(ref)
    except EntityNotFound:
        return None


def _extract_resources(ref, resources):
    """Extract data from resources metadata.
        :param ref: The reference of the entity.
        :param resources: the resources metadata associated with the entity.
        :returns: a dictionary of resource name and an array containing
                the file extension, the file link of the resource.
    """
    result = {}
    for resource in resources:
        resource_url = ""
        if resource["Revision"] >= 0:
            resource_url = cs.resource_url(
                ref, resource["Name"], resource["Revision"]
            )
        result[resource["Name"]] = [
            os.path.splitext(resource["Path"])[1],
            resource_url,
        ]

    return result


def _get_display_name(name):
    """Clean the name of the charm for readability.
        :param name the charm/bundle name.
        :return a cleaned name for display.
    """
    name = name.replace("-", " ")
    # Hack to rename 'canonical kubernetes'. To be removed when display
    # name has been implemented.
    if name == "canonical kubernetes":
        name = "The Charmed Distribution of Kubernetes"
    return name


def _get_bug_url(name, bugs_url):
    """Create a link to the bug tracker on Launchpad.
        :param name: the charm name.
        :returns: a URL for the bug tracker.
    """
    if bugs_url:
        return bugs_url
    return "https://bugs.launchpad.net/charms/+source/{}".format(name)


def _render_markdown(content):
    html = gfm.markdown(content)

    try:
        html = _convert_http_to_https(html)
    except Exception:
        # Leave the readme unparsed.
        pass

    return html


def _convert_http_to_https(content):
    """Convert any non secure inclusion of assets to secure.
        :param content: the content to parse as a string.
        :returns: the parsed content with http replaces with https
    """
    insensitive_link = re.compile(re.escape('src="http:'), re.IGNORECASE)
    content = insensitive_link.sub('src="https:', content)
    insensitive_link = re.compile(re.escape("src='http:"), re.IGNORECASE)
    content = insensitive_link.sub("src='https:", content)
    return content


def _extract_from_extrainfo(charm_data, ref):
    extra_info = charm_data.get("extra-info", {})
    revisions = extra_info.get("bzr-revisions") or extra_info.get(
        "vcs-revisions"
    )
    bzr_url = extra_info.get("bzr-url")
    supported = bool(extra_info.get("supported", False))
    supported_price = extra_info.get("price")
    supported_description = extra_info.get("description")
    return (
        bzr_url,
        revisions,
        supported,
        supported_price,
        supported_description,
    )


def _extract_from_commoninfo(bundle_data):
    common_info = bundle_data.get("common-info", {})
    bugs_url = common_info.get("bugs-url")
    homepage = common_info.get("homepage")
    return bugs_url, homepage
