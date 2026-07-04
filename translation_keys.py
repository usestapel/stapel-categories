"""
Translation key collection for catalog entities.

Collects all translation keys from:
- Categories (recursive tree-walk order)
- Features (recursive tree-walk order)
- Feature config options (select, hierarchical_select, etc.)
- String feature values
"""

from typing import Set, Dict, List, Any
from .models import Category, Feature


def _build_category_path(category: Category) -> str:
    """
    Build hierarchical path for category with IDs and names.

    Format:
    rootId: rootName
    =subId: subName
    ==subSubID: subSubName
    ===thisId: this name
    """
    ancestors_map = {a.id: a for a in category.get_ancestors_queryset()}

    # Walk parent chain from category to root, then reverse
    chain = [category]
    parent_id = category.tn_parent_id
    while parent_id and parent_id in ancestors_map:
        chain.append(ancestors_map[parent_id])
        parent_id = ancestors_map[parent_id].tn_parent_id
    chain.reverse()  # Root first

    lines = []
    for i, cat in enumerate(chain):
        prefix = '=' * i
        lines.append(f"{prefix}{cat.id}: {cat.name}")

    return '\n'.join(lines)


def _build_feature_path(feature: Feature) -> str:
    """
    Build path for feature with ID path and name.

    Format: ID: 1.2.3 Name: featureName
    """
    ancestors_map = {a.id: a for a in feature.get_ancestors_queryset()}

    # Walk parent chain from feature to root, then reverse
    chain = [feature]
    parent_id = feature.tn_parent_id
    while parent_id and parent_id in ancestors_map:
        chain.append(ancestors_map[parent_id])
        parent_id = ancestors_map[parent_id].tn_parent_id
    chain.reverse()  # Root first

    path = '.'.join(str(node.id) for node in chain)
    return f"ID: {path} Name: {feature.name}"


def collect_category_translation_keys_with_refs() -> List[Dict]:
    """
    Collect all translation keys from categories with refs and hierarchical comments.
    Uses recursive tree-walk order (depth-first, ordered by tn_priority desc).

    Returns:
        List of dicts with 'key', 'comment', 'refs', and 'order' fields
    """
    # Build ordered list of categories via recursive tree-walk
    ordered_categories: List[Category] = []

    def walk_categories(parent=None):
        if parent is None:
            roots = Category.objects.filter(
                tn_parent__isnull=True
            ).select_related('tn_parent').order_by('-tn_priority')
        else:
            roots = parent.get_children_queryset().select_related(
                'tn_parent'
            ).order_by('-tn_priority')

        for cat in roots:
            if cat.name and cat.translatable:
                ordered_categories.append(cat)
            # Always recurse into children regardless of translatable
            walk_categories(cat)

    walk_categories()

    # Group by key, preserving first-seen order
    key_to_data: Dict[str, Dict] = {}
    key_order: List[str] = []

    for category in ordered_categories:
        key = category.name
        ref = f"/catalog/admin/categories/category/{category.id}/"

        if key not in key_to_data:
            key_to_data[key] = {'refs': [], 'comment_parts': [], 'short_comments': []}
            key_order.append(key)

        data = key_to_data[key]
        data['refs'].append(ref)

        idx = len(data['refs'])
        section = f"Ref {idx}\n{_build_category_path(category)}"
        if category.comment:
            section += f"\nComment: {category.comment}"
            if category.comment not in data['short_comments']:
                data['short_comments'].append(category.comment)
        data['comment_parts'].append(section)

    result = []
    for order_idx, key in enumerate(key_order):
        data = key_to_data[key]
        result.append({
            'key': key,
            'comment': '\n------\n'.join(data['comment_parts']),
            'translator_comment': '; '.join(data['short_comments']),
            'refs': data['refs'],
            'order': order_idx,
        })

    return result


def collect_category_translation_keys() -> Set[str]:
    """
    Collect all translation keys from categories.

    Returns:
        Set of translation keys
    """
    return {item['key'] for item in collect_category_translation_keys_with_refs()}


def _extract_option_keys_as_list(config: dict) -> List[str]:
    """
    Extract translation keys from feature config options, preserving order.

    Returns:
        List of option label translation keys in config order
    """
    keys: List[str] = []
    seen: Set[str] = set()

    if not config:
        return keys

    feature_type = config.get('type')

    if feature_type == 'select':
        if not config.get('translatable_options', True):
            return keys
        for option in config.get('options', []):
            if isinstance(option, dict) and 'label' in option:
                label = option['label']
                if label and label not in seen:
                    keys.append(label)
                    seen.add(label)

    elif feature_type == 'hierarchical_select':
        if not config.get('translatable_options', True):
            return keys
        _collect_hierarchical_option_keys_ordered(config.get('options', []), keys, seen)

    elif feature_type == 'string':
        for suggestion in config.get('suggestions', []):
            if isinstance(suggestion, str) and suggestion and suggestion not in seen:
                keys.append(suggestion)
                seen.add(suggestion)
        for option in config.get('options', []):
            if isinstance(option, str) and option and option not in seen:
                keys.append(option)
                seen.add(option)

    return keys


def _collect_hierarchical_option_keys_ordered(
    options: List[dict], keys: List[str], seen: Set[str]
):
    """Recursively collect keys from hierarchical options in order."""
    for option in options:
        if isinstance(option, dict):
            if 'label' in option and option['label'] and option['label'] not in seen:
                keys.append(option['label'])
                seen.add(option['label'])
            if 'childrenTitle' in option and option['childrenTitle'] and option['childrenTitle'] not in seen:
                keys.append(option['childrenTitle'])
                seen.add(option['childrenTitle'])
            if 'children' in option and isinstance(option['children'], list):
                _collect_hierarchical_option_keys_ordered(option['children'], keys, seen)


def collect_feature_translation_keys_with_refs() -> List[Dict]:
    """
    Collect all translation keys from features with refs and comments.
    Uses recursive tree-walk order (depth-first, ordered by tn_priority desc).

    For each feature, keys are collected in this order:
    1. Feature name (if translate mode = TITLE or ALL)
    2. Config UI keys: placeholder, prefix, postfix, postfix1000
    3. Option labels (select/hierarchical_select, preserving order)
    4. Recurse into child features

    Returns:
        List of dicts with 'key', 'comment', 'refs', and 'order' fields
    """
    # Ordered list of (key, feature, is_option, all_options_list)
    ordered_items: List[tuple] = []

    def walk_features(parent=None):
        if parent is None:
            roots = Feature.objects.filter(
                tn_parent__isnull=True
            ).select_related('tn_parent').order_by('-tn_priority')
        else:
            roots = parent.get_children_queryset().select_related('tn_parent').order_by('-tn_priority')

        for feature in roots:
            if feature.translate == Feature.TranslateMode.NONE:
                walk_features(feature)
                continue

            # 1. Feature name
            if feature.translate in [Feature.TranslateMode.TITLE, Feature.TranslateMode.ALL]:
                if feature.name:
                    ordered_items.append((feature.name, feature, False, None))

            # 2. Config UI keys + 3. Option labels
            if feature.translate == Feature.TranslateMode.ALL:
                config = feature.config or {}

                # Config UI keys
                for ui_key_field in ['placeholder', 'prefix', 'postfix', 'postfix1000']:
                    ui_val = config.get(ui_key_field)
                    if ui_val and isinstance(ui_val, str):
                        ordered_items.append((ui_val, feature, True, None))

                # Option labels in order
                option_keys_list = _extract_option_keys_as_list(config)
                for opt_key in option_keys_list:
                    ordered_items.append((opt_key, feature, True, option_keys_list))

            # 4. Recurse into children
            walk_features(feature)

    walk_features()

    # Group by key, preserving first-seen order
    key_to_data: Dict[str, Dict] = {}
    key_order: List[str] = []

    for key, feature, is_option, all_options in ordered_items:
        ref = f"/catalog/admin/categories/feature/{feature.id}/"

        if key not in key_to_data:
            key_to_data[key] = {'refs': [], 'comment_parts': [], 'short_comments': []}
            key_order.append(key)

        data = key_to_data[key]
        if ref not in data['refs']:
            data['refs'].append(ref)

        section = f"Ref {len(data['refs'])}\n{_build_feature_path(feature)}"
        if is_option and all_options:
            other_options = [opt for opt in all_options if opt != key]
            if other_options:
                section += f"\nOther options: {', '.join(other_options)}"
        if feature.comment:
            section += f"\nComment: {feature.comment}"
            if feature.comment not in data['short_comments']:
                data['short_comments'].append(feature.comment)

        if section not in data['comment_parts']:
            data['comment_parts'].append(section)

    result = []
    for order_idx, key in enumerate(key_order):
        data = key_to_data[key]
        result.append({
            'key': key,
            'comment': '\n------\n'.join(data['comment_parts']),
            'translator_comment': '; '.join(data['short_comments']),
            'refs': data['refs'],
            'order': order_idx,
        })

    return result


def collect_feature_translation_keys() -> Set[str]:
    """
    Collect all translation keys from features.

    Returns:
        Set of translation keys
    """
    return {item['key'] for item in collect_feature_translation_keys_with_refs()}


def _extract_option_keys_from_config(config: dict) -> Set[str]:
    """
    Extract translation keys from feature config options.

    Returns:
        Set of option label translation keys
    """
    return set(_extract_option_keys_as_list(config))


def _extract_hierarchical_option_keys(options: List[dict], keys: Set[str] | None = None) -> Set[str]:
    """
    Recursively extract translation keys from hierarchical options.
    """
    if keys is None:
        keys = set()

    for option in options:
        if isinstance(option, dict):
            if 'label' in option and option['label']:
                keys.add(option['label'])
            if 'childrenTitle' in option and option['childrenTitle']:
                keys.add(option['childrenTitle'])
            if 'children' in option and isinstance(option['children'], list):
                _extract_hierarchical_option_keys(option['children'], keys)

    return keys


def collect_all_catalog_translation_keys() -> Dict[str, Any]:
    """
    Collect all translation keys from catalog.

    Returns:
        Dict with keys grouped by source:
        {
            'categories': [...],  # Keys with refs, comments, and order from categories
            'features': [...],    # Keys with refs, comments, and order from features
            'all_keys': [...],    # All unique keys combined
            'total_count': int    # Total unique keys
        }
    """
    category_keys_with_refs = collect_category_translation_keys_with_refs()
    feature_keys_with_refs = collect_feature_translation_keys_with_refs()

    # Extract just the keys for all_keys
    category_keys_set = {item['key'] for item in category_keys_with_refs}
    feature_keys_set = {item['key'] for item in feature_keys_with_refs}
    all_keys = category_keys_set | feature_keys_set

    return {
        'categories': category_keys_with_refs,
        'features': feature_keys_with_refs,
        'all_keys': sorted(list(all_keys)),
        'total_count': len(all_keys),
    }
