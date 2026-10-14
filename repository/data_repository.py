"""
DataRepository - Handles loading and managing CSV timetable data.

This class encapsulates all CSV file operations in one place, making it easy
to swap data sources later (e.g., from database, API, etc.) without changing
the rest of the app.
"""

import os
import pandas as pd


class DataRepository:
    """
    Loads and manages timetable data from CSV files.

    Attributes:
        data_dir (str): Path to the data directory containing CSV files.
        timetable_df (pd.DataFrame): The loaded timetable data.
    """

    def __init__(self, data_dir: str = "data"):
        """
        Initialize the DataRepository.

        Args:
            data_dir (str): Path to directory containing CSV files.
                          Defaults to "data" in current working directory.
        """
        self.data_dir = data_dir
        self.timetable_df = None

    def load_timetable(self, filename: str = "generated_timetable.csv") -> pd.DataFrame:
        """
        Load timetable data from a CSV file.

        Args:
            filename (str): Name of CSV file to load.
                          Defaults to "generated_timetable.csv".

        Returns:
            pd.DataFrame: Timetable data with columns:
                         [Class, Day, Period, Teacher]

        Raises:
            FileNotFoundError: If the CSV file does not exist.
            pd.errors.ParserError: If the CSV file is malformed.
        """
        filepath = os.path.join(self.data_dir, filename)

        if not os.path.exists(filepath):
            raise FileNotFoundError(
                f"Timetable file not found: {filepath}\n"
                f"Please ensure '{filename}' exists in the '{self.data_dir}' directory."
            )

        # Load the CSV into a DataFrame
        self.timetable_df = pd.read_csv(filepath)

        return self.timetable_df

    def get_timetable(self) -> pd.DataFrame:
        """
        Get the currently loaded timetable data.

        Returns:
            pd.DataFrame: The timetable data, or None if not yet loaded.
        """
        return self.timetable_df

    def get_unique_teachers(self) -> list:
        """
        Get a sorted list of unique teacher names from timetable.

        Returns:
            list: Sorted teacher names.

        Raises:
            ValueError: If timetable is not loaded yet.
        """
        if self.timetable_df is None:
            raise ValueError("Timetable not loaded. Call load_timetable() first.")

        teachers = sorted(self.timetable_df["Teacher"].unique().tolist())
        return teachers

    def get_unique_classes(self) -> list:
        """
        Get a sorted list of unique class names from timetable.

        Returns:
            list: Sorted class names.

        Raises:
            ValueError: If timetable is not loaded yet.
        """
        if self.timetable_df is None:
            raise ValueError("Timetable not loaded. Call load_timetable() first.")

        classes = sorted(self.timetable_df["Class"].unique().tolist())
        return classes

    def filter_by_teacher(self, teacher_name: str) -> pd.DataFrame:
        """
        Filter timetable by teacher name.

        Args:
            teacher_name (str): Name of teacher to filter by.

        Returns:
            pd.DataFrame: Filtered timetable rows.

        Raises:
            ValueError: If timetable is not loaded yet.
        """
        if self.timetable_df is None:
            raise ValueError("Timetable not loaded. Call load_timetable() first.")

        return self.timetable_df[self.timetable_df["Teacher"] == teacher_name]

    def filter_by_class(self, class_name: str) -> pd.DataFrame:
        """
        Filter timetable by class name.

        Args:
            class_name (str): Name of class to filter by.

        Returns:
            pd.DataFrame: Filtered timetable rows.

        Raises:
            ValueError: If timetable is not loaded yet.
        """
        if self.timetable_df is None:
            raise ValueError("Timetable not loaded. Call load_timetable() first.")

        return self.timetable_df[self.timetable_df["Class"] == class_name]

    def filter_by_teacher_and_class(
        self, teacher_name: str, class_name: str
    ) -> pd.DataFrame:
        """
        Filter timetable by both teacher name and class name.

        Args:
            teacher_name (str): Name of teacher to filter by.
            class_name (str): Name of class to filter by.

        Returns:
            pd.DataFrame: Filtered timetable rows.

        Raises:
            ValueError: If timetable is not loaded yet.
        """
        if self.timetable_df is None:
            raise ValueError("Timetable not loaded. Call load_timetable() first.")

        filtered = self.timetable_df[
            (self.timetable_df["Teacher"] == teacher_name) &
            (self.timetable_df["Class"] == class_name)
        ]
        return filtered
