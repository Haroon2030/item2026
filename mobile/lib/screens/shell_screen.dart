import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../state/app_state.dart';
import '../theme.dart';
import 'daily_screen.dart';
import 'groups_screen.dart';

class ShellScreen extends StatefulWidget {
  const ShellScreen({super.key, required this.state});

  final AppState state;

  @override
  State<ShellScreen> createState() => _ShellScreenState();
}

class _ShellScreenState extends State<ShellScreen> {
  int _tab = 0;

  AppState get state => widget.state;

  Future<void> _pickRange() async {
    final picked = await showDateRangePicker(
      context: context,
      firstDate: DateTime(2024),
      lastDate: DateTime.now().add(const Duration(days: 1)),
      initialDateRange: DateTimeRange(start: state.dateFrom, end: state.dateTo),
      locale: const Locale('ar'),
    );
    if (picked != null) {
      await state.setRange(picked.start, picked.end);
    }
  }

  Future<void> _pickBranch() async {
    final selected = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (context) {
        return ListView(
          children: [
            ListTile(
              title: const Text('كل الفروع'),
              selected: state.branch.isEmpty,
              onTap: () => Navigator.pop(context, ''),
            ),
            for (final item in state.branches)
              ListTile(
                title: Text(item.name),
                subtitle: Text(item.code),
                selected: state.branch == item.code,
                onTap: () => Navigator.pop(context, item.code),
              ),
          ],
        );
      },
    );
    if (selected != null) {
      await state.setBranch(selected);
    }
  }

  @override
  Widget build(BuildContext context) {
    final fmt = DateFormat('d MMM', 'ar');
    final sameDay = state.dateFromText == state.dateToText;
    final rangeLabel = sameDay
        ? 'اليوم · ${fmt.format(state.dateFrom)}'
        : '${fmt.format(state.dateFrom)} — ${fmt.format(state.dateTo)}';
    var branchLabel = 'كل الفروع';
    if (state.branch.isNotEmpty) {
      final match = state.branches.where((b) => b.code == state.branch);
      branchLabel = match.isEmpty ? 'فرع ${state.branch}' : match.first.name;
    }

    return Scaffold(
      appBar: AppBar(
        title: Column(
          children: [
            Text(state.user?.displayName ?? 'مبيعات الرشيد'),
            Text(
              state.user?.roleName ?? '',
              style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w400),
            ),
          ],
        ),
        actions: [
          IconButton(
            tooltip: 'اليوم',
            onPressed: state.setToday,
            icon: const Icon(Icons.today_outlined),
          ),
          IconButton(
            tooltip: 'خروج',
            onPressed: () => state.logout(),
            icon: const Icon(Icons.logout),
          ),
        ],
      ),
      body: Column(
        children: [
          Material(
            color: AppColors.brandDark,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
              child: Row(
                children: [
                  Expanded(
                    child: _FilterChip(
                      icon: Icons.date_range,
                      label: rangeLabel,
                      onTap: _pickRange,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: _FilterChip(
                      icon: Icons.store_mall_directory_outlined,
                      label: branchLabel,
                      onTap: _pickBranch,
                    ),
                  ),
                ],
              ),
            ),
          ),
          Expanded(
            child: IndexedStack(
              index: _tab,
              children: [
                DailyScreen(state: state),
                GroupsScreen(state: state),
              ],
            ),
          ),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (i) => setState(() => _tab = i),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.today_outlined),
            selectedIcon: Icon(Icons.today),
            label: 'اليوم',
          ),
          NavigationDestination(
            icon: Icon(Icons.category_outlined),
            selectedIcon: Icon(Icons.category),
            label: 'المجموعات',
          ),
        ],
      ),
    );
  }
}

class _FilterChip extends StatelessWidget {
  const _FilterChip({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: const Color(0x22FFFFFF),
      borderRadius: BorderRadius.circular(12),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
          child: Row(
            children: [
              Icon(icon, color: Colors.white, size: 18),
              const SizedBox(width: 6),
              Expanded(
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Colors.white,
                    fontWeight: FontWeight.w700,
                    fontSize: 13,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
